# -*- coding: utf-8 -*-
# Copyright 2018, 2019 Ciena Corporation
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

"""
Endpoints for testing
"""

from fnmatch import fnmatchcase
from functools import partial
from pprint import pformat
from struct import Struct

import attr
from twisted.internet import defer
from twisted.internet.interfaces import IAddress, IStreamClientEndpoint
from twisted.internet.protocol import connectionDone
from twisted.logger import Logger
from twisted.protocols.basic import Int32StringReceiver
from twisted.test import iosim
from zope.interface import implementer


@implementer(IStreamClientEndpoint)
@attr.s(frozen=True)
class FailureEndpoint(object):
    """
    Immediately fail every connection attempt.
    """

    reactor = attr.ib()
    host = attr.ib()
    port = attr.ib()

    def connect(self, protocolFactory):
        e = IndentationError("failing to connect {}".format(protocolFactory))
        return defer.fail(e)


@implementer(IStreamClientEndpoint)
@attr.s(frozen=True)
class BlackholeEndpoint(object):
    """
    Black-hole every connection attempt by returning a deferred which never
    fires.
    """

    reactor = attr.ib()
    host = attr.ib()
    port = attr.ib()

    def connect(self, protocolFactory):
        return defer.Deferred()


@implementer(IAddress)
@attr.s(frozen=True)
class _DebugAddress(object):
    host = attr.ib()
    port = attr.ib()


@attr.s(frozen=True, eq=False)
class Connections(object):
    """Externally controllable endpoint factory

    Each time `Connections` is called to generate an endpoint it returns one
    that records each connection attempt and returns an unresolved deferred.

    :ivar calls:
        List of (host, port) tuples, one for each call to construct an
        endpoint.

    :ivar connects:
        List of (host, port, protocolFactory, deferred), one for each call to
        an endpoint ``connect()`` method.
    """

    calls = attr.ib(init=False, default=attr.Factory(list))
    connects = attr.ib(init=False, default=attr.Factory(list))
    _pumps = attr.ib(init=False, repr=False, default=attr.Factory(list))

    def __call__(self, reactor, host, port):
        self.calls.append((host, port))
        return _PuppetEndpoint(partial(self._connect, host, port))

    def _connect(self, host, port, protocolFactory):
        d = defer.Deferred()
        self.connects.append((host, port, protocolFactory, d))
        return d

    def _match(self, host_pattern):
        for index in range(len(self.connects)):
            host = self.connects[index][0]
            if fnmatchcase(host, host_pattern):
                break
        else:
            raise AssertionError(
                "No match for host pattern {!r}. Searched: {}".format(host_pattern, pformat(self.connects))
            )
        return self.connects.pop(index)

    def accept(self, host_pattern):
        """
        Accept a pending connection request.

        :param str host_pattern:
            :func:`fnmatch.fnmatchcase` pattern against which the connection
            request must match.

        :returns: `KafkaConnection`
        """
        host, port, protocolFactory, d = self._match(host_pattern)
        peerAddress = _DebugAddress(host, port)
        client_protocol = protocolFactory.buildProtocol(peerAddress)
        assert client_protocol is not None
        server_protocol = KafkaBrokerProtocol()
        client_transport = iosim.FakeTransport(client_protocol, isServer=False, peerAddress=peerAddress)
        server_transport = iosim.FakeTransport(server_protocol, isServer=True)
        pump = iosim.connect(server_protocol, server_transport, client_protocol, client_transport)
        self._pumps.append(pump)
        d.callback(client_protocol)
        return KafkaConnection(
            server=server_protocol,
            client=client_protocol,
            pump=pump,
        )

    def fail(self, host_pattern, reason):
        """
        Fail a pending connection request.

        :param str host_pattern:
            :func:`fnmatch.fnmatchcase` pattern against which the connection
            request must match.

        :param reason: `twisted.python.failure.Failure`
        """
        host, port, protocolFactory, d = self._match(host_pattern)
        d.errback(reason)

    def flush(self):
        """
        Flush I/O on all accepted connections.
        """
        for pump in self._pumps:
            pump.flush()


@implementer(IStreamClientEndpoint)
@attr.s(frozen=True)
class _PuppetEndpoint(object):
    """
    Implementation detail of `Connections`.
    """

    connect = attr.ib()


@attr.s(frozen=True, eq=False)
class KafkaConnection(object):
    """
    Controller for a connection accepted by `Connections`
    """

    #: `twisted.test.iosim.FakeTransport` (client mode)
    client = attr.ib()
    #: `twisted.test.iosim.FakeTransport` (server mode)
    server = attr.ib()
    #: `twisted.test.iosim.IOPump` that flushes data between *client* and *server*
    pump = attr.ib()


@attr.s(frozen=True)
class Request(object):
    _protocol = attr.ib(repr=False)
    api_key = attr.ib()
    api_version = attr.ib()
    correlation_id = attr.ib()
    client_id = attr.ib()
    rest = attr.ib()

    _req_header = Struct(">hhih")
    _resp_header = Struct(">i")

    @classmethod
    def from_bytes(cls, protocol, request):
        """
        :param protocol: `KafkaBrokerProtocol` object
        :param bytes request: Kafka request, less length prefix
        """
        size = cls._req_header.size
        api_key, api_version, correlation_id, client_id_len = cls._req_header.unpack(request[:size])
        if client_id_len < 0:  # Nullable string.
            header_end = size
            client_id = None
        else:
            header_end = size + client_id_len
            client_id = request[size:header_end].decode("utf-8")
        return cls(
            protocol,
            api_key,
            api_version,
            correlation_id,
            client_id,
            request[header_end:],
        )

    def respond(self, response):
        r = self._resp_header.pack(self.correlation_id) + response
        self._protocol.sendString(r)


class KafkaBrokerProtocol(Int32StringReceiver):
    """The server side of the Kafka protocol

    Like a real Kafka broker it handles one request at a time.

    :ivar requests:
    """

    _log = Logger()
    MAX_LENGTH = 2**16  # Small limit for testing.
    disconnected = None

    def connectionMade(self):
        self._log.debug("Connected")
        self.requests = defer.DeferredQueue(backlog=1)

    def stringReceived(self, request):
        """
        Handle a request from the broker.
        """
        r = Request.from_bytes(self, request)
        self._log.debug("Received {request}", request=r)
        self.requests.put(r)

    def connectionLost(self, reason=connectionDone):
        self.disconnected = reason
        self._log.debug("Server connection lost: {reason}", reason=reason)

    def expectRequest(self, api_key, api_version, correlation_id):
        """
        Assert that the next request to enter the queue matches the parameters
        and return it.
        """

        def assert_(request):
            expected = (
                api_key,
                api_version,
                correlation_id,
            )
            actual = (
                request.api_key,
                request.api_version,
                request.correlation_id,
            )
            if expected != actual:
                raise AssertionError(
                    (
                        "Request {!r} doesn't match expectation of api_key={!r},"
                        " api_version={!r}, correlation_id={!r}"
                    ).format(request, api_key, api_version, correlation_id)
                )

            return request

        return self.requests.get().addCallback(assert_)
