# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2017, 2018, 2019, 2021 Ciena Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""KafkaBrokerClient and private _Request classes.

Low level network client for the Apache Kafka Message Broker.
"""

import logging
import reprlib
from collections import OrderedDict
from datetime import datetime
from functools import partial

import attr
from ._protocol import KafkaProtocol
from .common import ClientError, DuplicateRequestError
from .kafkacodec import KafkaCodec, _ReprRequest
from twisted.internet.defer import Deferred, fail, maybeDeferred
from twisted.internet.protocol import ClientFactory
from twisted.internet.task import deferLater
from twisted.python.failure import Failure

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

MAX_RECONNECT_DELAY_SECONDS = 15
INIT_DELAY_SECONDS = 0.1


@attr.s(slots=True)
class _RequestState(object):
    """
    Private helper class to hold flags about in-flight requests.

    :ivar int correlationId: Unique-per-connection ID number for the request.

    :ivar bytes request: Serialized Kafka PDU, less the length prefix.

    :ivar bool expectResponse:
        Is a response expected for this request? If not, it will be discarded
        once it has been written to the protocol.

    :ivar datetime queued:
        When was `_KafkaBrokerClient.makeRequest()` called?

    :ivar datetime sent:
        When was this request written to a connection? `None` if that hasn't
        happened yet.  A request that hasn't been written will be discarded
        immediately (never sent) when cancelled.

    :ivar datetime cancelled:
        When was the deferred for this request canceled? `None` if it hasn't
        been. The queue entry for a cancelled request is kept around if it has
        been sent so that the response correlation logic can differentiate
        between responses that were too late vs. responses to requests that
        were never sent.
    """

    correlationId = attr.ib()
    request = attr.ib(repr=False)
    expectResponse = attr.ib()
    d = attr.ib()
    queued = attr.ib()
    sent = attr.ib(default=None)
    cancelled = attr.ib(default=None)


_aLongerRepr = reprlib.Repr()
_aLongerRepr.maxother = 1024  # bytes is not str, counts as "other"


class _KafkaBrokerClient(ClientFactory):
    """
    The low-level client which handles transport to a single Kafka broker.

    The KafkaBrokerClient object is responsible for maintaining a connection to
    a single Kafka broker, reconnecting as needed, over which it sends requests
    and receives responses. Callers make requests with :py:method:`makeRequest`
    """

    protocol = KafkaProtocol

    # Reduce log spam from twisted
    noisy = False

    def __init__(self, reactor, endpointFactory, brokerMetadata, clientId, retryPolicy):
        """
        Create a client for a specific broker

        The broker client connects to the broker as needed to handle requests.
        Requests are retried when the connection fails before the client
        receives the response. Requests can be cancelled at any time.

        :param reactor: Twisted reactor to use for connecting and when scheduling
                delayed calls.

        :param endpointFactory:
            Callable that accepts (reactor, host, port) as arguments and
            returns an IStreamClientEndpoint to use to connect to the broker.

        :param BrokerMetadata brokerMetadata:
            Broker node ID, host, and port. This may be updated later by
            calling updateMetadata().

        :param str clientId: Identifying string for log messages. NOTE: not the
            ClientId in the RequestMessage PDUs going over the wire.
        """
        self._reactor = reactor  # ReconnectingClientFactory uses self.clock.
        self._endpointFactory = endpointFactory
        self.node_id = brokerMetadata.node_id
        self.host = brokerMetadata.host
        self.port = brokerMetadata.port
        self.clientId = clientId
        self._retryPolicy = retryPolicy

        # No connector until we try to connect
        self.connector = None
        # The protocol object for the current connection
        self.proto = None
        # ordered dict of _Requests, keyed by requestId
        self.requests = OrderedDict()
        # deferred which fires when the close() completes
        self._dDown = None

    def __repr__(self):
        """return a string representing this KafkaBrokerClient."""
        return "_KafkaBrokerClient<node_id={} {}:{} {}>".format(
            self.node_id,
            self.host,
            self.port,
            "connected" if self.connected() else "unconnected",
        )

    def updateMetadata(self, new):
        """
        Update the metadata stored for this broker.

        Future connections made to the broker will use the host and port
        defined in the new metadata. Any existing connection is not dropped,
        however.

        :param new:
            :clas:`afkak.common.BrokerMetadata` with the same node ID as the
            current metadata.
        """
        if self.node_id != new.node_id:
            raise ValueError("Broker metadata {!r} doesn't match node_id={}".format(new, self.node_id))

        self.node_id = new.node_id
        self.host = new.host
        self.port = new.port

    def makeRequest(self, correlationId, request, expectResponse=True):
        """
        Send a request to this broker.

        The request may not be sent immediately, but it should be sent soon.
        A connection to the broker is created when the first request is made.

        Each request is sent at most once per broker connection. If the
        connection drops while requests are outstanding then requests are
        resent upon reconnection in the order originally issued.

        This method returns a deferred that fires with the `bytes` of the
        broker response. It may fail in a few ways:

        - With `ClientError` when the `_KafkaBrokerClient` is closed.
        - With `twisted.internet.defer.CancelledError` if its :meth:`cancel()
          <twisted.internet.defer.Deferred.cancel>`_ method is called.
        - With some other exception in the case of bugs.

        Cancelling the deferred _may_ prevent the request from being sent to
        a broker.

        :param int correlationId:
            ID number used to match responses with requests. This must match
            the number embedded in *request*.

        :param bytes request:
            The serialized request PDU (not including the length prefix).

        :parma bool expectResponse:
            Will the Kafka broker send a response? Unsetting this flag changes
            the result:

              - The request is considered sent as soon as it has been enqueued
                for write to a connection. It will never be resent, and will be
                lost if the connection drops before the broker processes it.
              - The returned deferred will fire with `None` instead of
                a response.

        :returns: Deferred that fires when the request has been processed

        :raises DuplicateRequestError: when *correlationId* is reused. This
            represents a programming error on the caller's part.
        """
        if correlationId in self.requests:
            # Id is duplicate to 'in-flight' request. Reject it, as we
            # won't be able to properly deliver the response(s)
            # Note that this won't protect against a client calling us
            # twice with the same ID, but first with expectResponse=False
            # But that's pathological, and the only defense is to track
            # all requestIds sent regardless of whether we expect to see
            # a response, which is effectively a memory leak...
            raise DuplicateRequestError("Reuse of correlationId={}".format(correlationId))

        # If we've been told to shutdown (close() called) then fail request
        if self._dDown:
            return fail(
                ClientError(
                    "Broker client for node_id={} {}:{} has been closed".format(self.node_id, self.host, self.port)
                )
            )

        # Ok, we are going to save/send it, create a _Request object to track
        self.requests[correlationId] = tReq = _RequestState(
            correlationId,
            request,
            expectResponse,
            d=Deferred(partial(self._cancelRequest, correlationId)),
            queued=datetime.utcfromtimestamp(self._reactor.seconds()),
        )

        # Do we have a connection over which to send the request?
        if self.proto:
            # Send the request
            # TODO: Flow control
            self._sendRequest(tReq)
        # Have we not even started trying to connect yet? Do so now
        elif not self.connector:
            self._connect()
        return tReq.d

    def disconnect(self):
        """
        Disconnect from the Kafka broker.

        This is used to implement disconnection on timeout as a workaround for
        Kafka connections occasionally getting stuck on the server side under
        load. Requests are not cancelled, so they will be retried.
        """
        if self.proto:
            log.debug("%r Disconnecting from %r", self, self.proto.transport.getPeer())
            self.proto.transport.loseConnection()

    def close(self):
        """Permanently dispose of the broker client.

        This terminates any outstanding connection and cancels any pending
        requests.
        """
        log.debug("%r: close() proto=%r connector=%r", self, self.proto, self.connector)
        assert self._dDown is None
        self._dDown = Deferred()

        if self.proto is not None:
            self.proto.transport.loseConnection()
        elif self.connector is not None:

            def connectingFailed(reason):
                """
                Handle the failure resulting from cancellation.

                :reason: a `Failure`, most likely a cancellation error (but
                    that's not guaranteed).
                :returns: `None` to handle the failure
                """
                log.debug("%r: connection attempt has been cancelled: %r", self, reason)
                self._dDown.callback(None)

            self.connector.addErrback(connectingFailed)
            self.connector.cancel()
        else:
            # Fake a cleanly closing connection
            self._dDown.callback(None)

        try:
            raise ClientError(
                "Broker client for node_id={} {}:{} was closed".format(self.node_id, self.host, self.port)
            )
        except Exception:
            reason = Failure()
        # Cancel any requests
        while self.requests:
            correlationId, tReq = self.requests.popitem(True)
            if tReq.cancelled is None:
                tReq.d.errback(reason)
        return self._dDown

    def connected(self):
        """Are we connected to a Kafka broker?"""
        return self.proto is not None

    def _connectionLost(self, reason):
        """Called when the protocol connection is lost

        - Log the disconnection.
        - Mark any outstanding requests as unsent so they will be sent when
          a new connection is made.
        - If closing the broker client, mark completion of that process.

        :param reason:
            Failure that indicates the reason for disconnection.
        """
        log.info("%r: Connection closed: %r", self, reason)

        # Reset our proto so we don't try to send to a down connection
        self.proto = None

        # Mark any in-flight requests as unsent, discard cancelled requests.
        for tReq in list(self.requests.values()):
            if tReq.cancelled is not None:
                del self.requests[tReq.correlationId]
            else:
                tReq.sent = None

        if self._dDown:
            self._dDown.callback(None)
        elif self.requests:
            self._connect()

    def handleResponse(self, response):
        """Handle the response string received by KafkaProtocol.

        Ok, we've received the response from the broker. Find the requestId
        in the message, lookup & fire the deferred with the response.
        """
        correlationId = KafkaCodec.get_response_correlation_id(response)
        # Protect against responses coming back we didn't expect
        tReq = self.requests.pop(correlationId, None)
        if tReq is None:
            # The broker sent us a response to a request we didn't make.
            log.error(
                "Unexpected response with correlationId=%d: %s",
                correlationId,
                _aLongerRepr.repr(response),
            )
        elif tReq.cancelled is not None:
            now = datetime.utcfromtimestamp(self._reactor.seconds())
            log.debug(
                "Response to %s arrived %s after it was cancelled (%d bytes)",
                _ReprRequest(tReq.request),
                now - tReq.cancelled,
                len(response),
            )
        else:
            tReq.d.callback(response)

    # # Private Methods # #

    def _sendRequest(self, tReq):
        """Send a single request over our protocol to the Kafka broker."""
        try:
            tReq.sent = datetime.utcfromtimestamp(self._reactor.seconds())
            self.proto.sendString(tReq.request)
        except Exception as e:
            log.exception("%r: Failed to send request %r", self, tReq)
            del self.requests[tReq.correlationId]
            tReq.d.errback(e)
        else:
            if not tReq.expectResponse:
                # Once we've sent a request for which we don't expect a reply,
                # we're done, remove it from requests, and fire the deferred
                # with 'None', since there is no reply to be expected
                del self.requests[tReq.correlationId]
                tReq.d.callback(None)

    def _sendQueued(self):
        """Connection just came up, send the unsent requests."""
        for tReq in list(self.requests.values()):  # must copy, may del
            if tReq.sent is None:
                self._sendRequest(tReq)

    def _cancelRequest(self, correlationId, deferred):
        """
        The ``cancel()`` method of a deferred returned by :meth:`makeRequest()`
        was called. If the request hasn't been sent, remove it from the queue.
        Otherwise it is flagged as cancelled so it won't be resent. The
        queue table entry is retained so if a response comes we don't log an
        error.
        """
        tReq = self.requests[correlationId]
        if tReq.sent is not None:
            tReq.cancelled = datetime.utcfromtimestamp(self._reactor.seconds())
        else:
            del self.requests[correlationId]

    def _abortRequest(self, correlationId, reason):
        """
        Remove a request from the queue and fail its deferred.

        :param int correlationId: Request ID

        :param reason: :class:`twisted.python.failure.Failure` instance to
            errback the request deferred with
        """
        tReq = self.requests.pop(correlationId)
        tReq.d.errback(reason)

    def _connect(self):
        """Connect to the Kafka Broker

        This routine will repeatedly try to connect to the broker (with backoff
        according to the retry policy) until it succeeds.
        """

        def tryConnect():
            self.connector = d = maybeDeferred(connect)
            d.addCallback(cbConnect)
            d.addErrback(ebConnect)

        def connect():
            endpoint = self._endpointFactory(self._reactor, self.host, self.port)
            log.debug("%r: connecting with %s", self, endpoint)
            return endpoint.connect(self)

        def cbConnect(proto):
            log.debug("%r: connected to %r", self, proto.transport.getPeer())
            self._failures = 0
            self.connector = None
            self.proto = proto
            if self._dDown:
                proto.transport.loseConnection()
            else:
                self._sendQueued()

        def ebConnect(fail):
            if self._dDown:
                log.debug("%r: breaking connect loop due to %r after close()", self, fail)
                return fail
            self._failures += 1
            delay = self._retryPolicy(self._failures)
            log.debug(
                "%r: failure %d to connect -> %s; retry in %.2f seconds.",
                self,
                self._failures,
                fail.value,
                delay,
            )

            self.connector = d = deferLater(self._reactor, delay, lambda: None)
            d.addCallback(cbDelayed)

        def cbDelayed(result):
            tryConnect()

        self._failures = 0
        tryConnect()
