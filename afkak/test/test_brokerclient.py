# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2017, 2018, 2019 Ciena Corporation
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
Test the _KafkaBrokerClient class.
"""

import logging

from ..brokerclient import _KafkaBrokerClient
from ..brokerclient import log as brokerclient_log
from ..common import BrokerMetadata, ClientError, DuplicateRequestError
from ..kafkacodec import KafkaCodec
from .endpoints import Connections
from .logtools import capture_logging
from twisted.internet import defer
from twisted.internet.error import ConnectionRefusedError
from twisted.internet.task import Clock
from twisted.test import iosim
from twisted.trial.unittest import SynchronousTestCase

log = logging.getLogger(__name__)


# The bytes of a trivial Kakfa request, a metadata request v0 for all topics
# with correlationId=1.
METADATA_REQUEST_1 = (
    b"\x00\x03"  # apiKey=METADATA
    b"\x00\x00"  # apiVersion=0
    b"\x00\x00\x00\x01"  # correlationId=1
    b"\xff\xff"  # clientId=null
    b"\x00\x00\x00\x00"  # 0 topics (meaning all topics)
)

# The bytes of a trivial Kakfa request, a metadata request v0 for all topics
# with correlationId=1.
METADATA_REQUEST_2 = (
    b"\x00\x03"  # apiKey=METADATA
    b"\x00\x00"  # apiVersion=0
    b"\x00\x00\x00\x02"  # correlationId=2
    b"\xff\xff"  # clientId=null
    b"\x00\x00\x00\x00"  # 0 topics (meaning all topics)
)

# The bytes of a trivial response to METADATA_REQUEST_1, an response with empty
# broker and topic lists. This is somewhat nonsensical (of course there must be
# brokers!) but _KafkaBrokerClient doesn't care.
METADATA_RESPONSE = b"\x00\x00\x00\x00" b"\x00\x00\x00\x00"  # Number of brokers (nonsense).  # Number of topics.


class BrokerClientTests(SynchronousTestCase):
    """Test `_KafkaBrokerClient`

    :ivar brokerClient: The `_KafkaBrokerClient` under test. It is configured
        with ``BrokerMetadata(node_id=1, host='host', port=1234)``.

    :ivar reactor: `Clock` used by the broker client for scheduling.

    :ivar connections: `afkak.test.endpoints.Connections` installed as the
        broker client's endpoint.

    :ivar float retryDelay: Seconds of delay between each connection attempt
        according to the broker client's retry policy. The default is 1.0. This
        may be mutated.
    """

    retryDelay = 1.0

    def setUp(self):
        self.reactor = Clock()
        self.connections = Connections()
        self.brokerClient = _KafkaBrokerClient(
            reactor=self.reactor,
            endpointFactory=self.connections,
            clientId="myclient",
            brokerMetadata=BrokerMetadata(
                node_id=1,
                host="host",
                port=1234,
            ),
            retryPolicy=lambda attempt: self.retryDelay,
        )

    def test_repr_init(self):
        """
        On initialization, the repr of the broker client shows the client ID
        and broker metadata.
        """
        self.assertEqual(
            "_KafkaBrokerClient<node_id=1 host:1234 unconnected>",
            repr(self.brokerClient),
        )

    def test_repr_connected(self):
        """
        Once connected, the repr shows the state as connected.
        """
        self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        self.connections.accept("*")

        self.assertEqual(
            "_KafkaBrokerClient<node_id=1 host:1234 connected>",
            repr(self.brokerClient),
        )

    def test_host_port(self):
        """
        The `host` and `port` properties reflect the broker metadata.
        """
        self.assertEqual("host", self.brokerClient.host)
        self.assertEqual(1234, self.brokerClient.port)

    def test_updateMetadata_retry(self):
        """
        Updating the broker metadata of the client changes the destination of
        the next connection attempt. Any outstanding connections remain until
        then.
        """
        d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        self.assertNoResult(d)
        self.assertEqual([("host", 1234)], self.connections.calls)

        self.brokerClient.updateMetadata(BrokerMetadata(node_id=1, host="other", port=2345))
        self.assertEqual("other", self.brokerClient.host)
        self.assertEqual(2345, self.brokerClient.port)
        # A connection to the new host was *not* triggered.
        self.assertEqual([("host", 1234)], self.connections.calls)

        # Fail the pending connection:
        self.connections.fail("host", ConnectionRefusedError("Nope."))
        # Trigger retry attempt, which happens after a delay:
        self.reactor.advance(self.retryDelay)

        # The retry attempt was made to the new host.
        self.assertEqual([("host", 1234), ("other", 2345)], self.connections.calls)

    def test_updateMetadata_invalid(self):
        """
        As a sanity check, a metadata update is rejected unless the node ID
        matches.
        """
        brokerMetadata = BrokerMetadata(node_id=2, host="other", port="2345")

        with self.assertRaises(ValueError):
            self.brokerClient.updateMetadata(brokerMetadata)

    def test_close_quiescent(self):
        """
        A quiescent broker client closes immediately.
        """
        self.assertIs(None, self.successResultOf(self.brokerClient.close()))

    def test_close_connecting(self):
        """
        When the broker client is closed during a connection attempt the
        connection attempt is cancelled.
        """
        request_d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)

        close_d = self.brokerClient.close()

        self.assertIs(None, self.successResultOf(close_d))
        f = self.failureResultOf(request_d, ClientError)
        self.assertEqual(
            "Broker client for node_id=1 host:1234 was closed",
            str(f.value),
        )

    def test_close_connecting_succeed(self):
        """
        If the endpoint connection attempt succeeds despite attempts to cancel
        it the connection is closed.

        This is pretty pathological.
        """
        cancels = []

        class ConnectOnCancelEndpoint(object):
            def __init__(self, reactor, host, port):
                pass

            def connect(self, protocolFactory):
                def connectOnCancel(deferred):
                    log.debug("%r cancelled", deferred)
                    protocol = protocolFactory.buildProtocol(None)
                    transport = iosim.FakeTransport(protocol, isServer=False)
                    cancels.append(transport)
                    protocol.makeConnection(transport)
                    deferred.callback(protocol)

                d = defer.Deferred(connectOnCancel)
                d.addBoth(self._logResult, d)
                log.debug("%r.connect() -> %r", self, d)
                return d

            def _logResult(self, result, d):
                log.debug("%r fired with result %r", d, result)
                return result

        brokerClient = _KafkaBrokerClient(
            reactor=Clock(),
            endpointFactory=ConnectOnCancelEndpoint,
            clientId="myclient",
            brokerMetadata=BrokerMetadata(node_id=1, host="host", port=1234),
            retryPolicy=lambda attempts: 1.0,
        )

        request_d = brokerClient.makeRequest(1, METADATA_REQUEST_1)
        self.assertEqual([], cancels)

        close_d = brokerClient.close()
        [transport] = cancels  # Was connected.
        self.assertTrue(transport.disconnecting)
        self.assertNoResult(close_d)
        self.failureResultOf(request_d, ClientError)

        transport.reportDisconnect()
        self.assertIs(None, self.successResultOf(close_d))

    def test_close_connected(self):
        """
        close() drops any open connection to the Kafka broker.
        """
        request_d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        conn = self.connections.accept("*")

        close_d = self.brokerClient.close()
        conn.pump.flush()  # Propagate connection loss.

        self.assertIs(None, self.successResultOf(close_d))
        self.failureResultOf(request_d, ClientError)

    def test_close_while_cancelled_in_flight(self):
        """
        close() may be called when a cancelled request is in flight. The
        request is cancelled as usual and the close completes successfully.
        """
        req_d = self.brokerClient.makeRequest(2, METADATA_REQUEST_2)
        conn = self.connections.accept("*")
        conn.pump.flush()

        req_d.cancel()
        close_d = self.brokerClient.close()
        conn.pump.flush()  # Propagate connection loss.

        self.failureResultOf(req_d, defer.CancelledError)
        self.assertIs(None, self.successResultOf(close_d))

    def test_disconnect_quiescent(self):
        """
        disconnect() has no effect when no connection is open.
        """
        self.brokerClient.disconnect()

    def test_disconnect_connected(self):
        """
        disconnect() drops any ongoing connection. A pending request triggers
        a reconnection attempt.
        """
        request_d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        conn1 = self.connections.accept("*")

        self.brokerClient.disconnect()
        conn1.pump.flush()
        self.assertTrue(conn1.server.transport.disconnected)  # Connection was dropped.
        self.assertNoResult(request_d)

        self.connections.accept("*")
        self.assertNoResult(request_d)

    def test_disconnect_no_requests(self):
        """
        disconnect() drops any ongoing connection. No reconnection is attempted
        when no requests are pending.
        """
        # Make a request to trigger a connection attempt, then cancel it so
        # that there aren't any pending requests.
        request_d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        conn1 = self.connections.accept("*")
        request_d.cancel()
        self.failureResultOf(request_d)

        self.brokerClient.disconnect()
        conn1.pump.flush()
        self.assertTrue(conn1.server.transport.disconnected)  # Connection was dropped.

        self.reactor.advance(self.retryDelay)  # Not necessary, but let's be sure.
        self.assertEqual([("host", 1234)], self.connections.calls)  # No further connection attempts.

    def test_makeRequest_closed(self):
        """
        makeRequest() fails with ClientError once the broker client has been
        closed.
        """
        self.successResultOf(self.brokerClient.close())

        d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)

        self.failureResultOf(d, ClientError)

    def test_makeRequest_duplicate(self):
        """
        makeRequest() raises DuplicateRequestError when presented with an
        obviously duplicate correlation ID.
        """
        d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        self.assertNoResult(d)

        with self.assertRaises(DuplicateRequestError):
            self.brokerClient.makeRequest(1, METADATA_REQUEST_1)

    def test_makeRequest_connecting(self):
        """
        makeRequest() waits for an ongoing connection attempt.
        """
        d1 = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        d2 = self.brokerClient.makeRequest(2, METADATA_REQUEST_2)

        conn = self.connections.accept("*")
        conn.pump.flush()

        self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, 1)).respond(METADATA_RESPONSE)
        self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, 2)).respond(METADATA_RESPONSE)
        conn.pump.flush()

        self.assertEqual(b"\x00\x00\x00\x01" + METADATA_RESPONSE, self.successResultOf(d1))
        self.assertEqual(b"\x00\x00\x00\x02" + METADATA_RESPONSE, self.successResultOf(d2))

    def test_makeRequest_connected(self):
        """
        makeRequest() reuses an extant connection.
        """
        d1 = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        conn = self.connections.accept("*")
        conn.pump.flush()
        self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, 1)).respond(METADATA_RESPONSE)
        conn.pump.flush()
        self.successResultOf(d1)

        d2 = self.brokerClient.makeRequest(2, METADATA_REQUEST_2)
        conn.pump.flush()
        self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, 2)).respond(METADATA_RESPONSE)
        conn.pump.flush()
        self.successResultOf(d2)

    def test_makeRequest_no_response(self):
        """
        makeRequest() doesn't keep around requests with
        ``expectResponse=False`` once they've been shoved into the OS socket
        buffer.
        """
        d1 = self.brokerClient.makeRequest(2, METADATA_REQUEST_2, expectResponse=False)
        conn = self.connections.accept("*")
        conn.pump.flush()

        self.assertIs(None, self.successResultOf(d1))
        self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, 2))

    def test_cancelled_response_received(self):
        """
        The received response is discarded if it arrives after the request has
        been cancelled. A debug log is emitted.
        """
        d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        conn = self.connections.accept("*")
        conn.pump.flush()
        req = self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, 1))

        d.cancel()
        self.failureResultOf(d, defer.CancelledError)

        self.reactor.advance(60.0 + 2.0)
        req.respond(METADATA_RESPONSE)

        with capture_logging(brokerclient_log) as records:
            conn.pump.flush()

        [record] = records
        self.assertEqual(logging.DEBUG, record.levelno)
        self.assertEqual(
            "Response to MetadataRequest0 correlationId=1 (14 bytes) arrived 0:01:02 after it was cancelled (12 bytes)",
            record.getMessage(),
        )

    def test_correlation_id_mismatch(self):
        """
        An error is logged when a response with an unexpected correlation ID
        is received.
        """
        d = self.brokerClient.makeRequest(1, METADATA_REQUEST_1)
        conn = self.connections.accept("*")

        conn.server.sendString((b"\x00\x00\x00\x03" b"some stuff"))  # correlationId=3

        with capture_logging(brokerclient_log) as records:
            conn.pump.flush()

        [record] = records
        self.assertEqual(logging.ERROR, record.levelno)
        self.assertTrue(record.getMessage().startswith("Unexpected response with correlationId=3: "))

        self.assertNoResult(d)  # Remains pending.
