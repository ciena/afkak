# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2018 Ciena Corporation
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

import logging
import unittest
from unittest import mock

from .._protocol import KafkaBootstrapProtocol, KafkaProtocol
from .logtools import capture_logging
from twisted.internet.address import IPv4Address
from twisted.internet.error import ConnectionDone, ConnectionLost
from twisted.logger import LogLevel, globalLogPublisher
from twisted.python.failure import Failure
from twisted.test.iosim import FakeTransport
from twisted.test.proto_helpers import EventLoggingObserver, StringTransportWithDisconnection
from twisted.trial.unittest import SynchronousTestCase


class TheFactory(object):
    """
    `TheFactory` implements the bits of `_KafkaBrokerClient` that
    `_KafkaProtocol` interacts with.
    """

    log = logging.getLogger(__name__).getChild("TheFactory")

    def handleResponse(self, string):
        """Called for each response."""

    def _connectionLost(self, reason):
        """Called when the connection goes down."""


class KafkaProtocolTests(unittest.TestCase):
    """Test `afkak._protocol.KafkaProtocol`"""

    def test_stringReceived(self):
        """
        The factory is notified of message receipt.
        """
        kp = KafkaProtocol()
        kp.factory = factory_spy = mock.Mock(wraps=TheFactory())

        kp.stringReceived(b"testing")

        factory_spy.handleResponse.assert_called_once_with(b"testing")

    def test_connectionLost_cleanly(self):
        """
        The factory is notified of connection loss.
        """
        kp = KafkaProtocol()
        kp.factory = factory_spy = mock.Mock(wraps=TheFactory())
        reason = Failure(ConnectionDone())

        kp.connectionLost(reason)

        factory_spy._connectionLost.assert_called_once_with(reason)
        self.assertIsNone(kp.factory)

    def test_lengthLimitExceeded(self):
        """
        An error is logged and the connection dropped when an oversized message
        is received.
        """
        too_long = KafkaProtocol.MAX_LENGTH + 1
        peer = IPv4Address("TCP", "1.2.3.4", 1234)
        kp = KafkaProtocol()
        kp.factory = factory_spy = mock.Mock(wraps=TheFactory())
        kp.transport = StringTransportWithDisconnection(peerAddress=peer)
        kp.transport.protocol = kp

        with capture_logging(logging.getLogger("afkak.protocol")) as records:
            kp.lengthLimitExceeded(too_long)

        self.assertEqual(1, len(factory_spy._connectionLost.mock_calls))

        [record] = records
        record.getMessage()  # Formats okay.
        self.assertEqual(
            ("Broker at %s sent a %d byte message, exceeding the size limit of %d. " "Terminating connection."),
            record.msg,
        )
        self.assertEqual((peer, too_long, kp.MAX_LENGTH), record.args)


class KafkaBootstrapProtocolTests(SynchronousTestCase):
    """Test `afkak._protocol.KafkaBootstrapProtocol`

    :ivar peer: Peer IAddress, an IPv4 address
    :ivar protocol: `KafkaBootstrapProtocol` object, connected in setUp
    :ivar transport: `FakeTransport` object associated with the protocol,
        connected in setUp
    """

    def setUp(self):
        self.peer = IPv4Address("TCP", "kafka", 9072)
        self.protocol = KafkaBootstrapProtocol()
        self.transport = FakeTransport(self.protocol, isServer=False, peerAddress=self.peer)
        self.protocol.makeConnection(self.transport)

    def test_one_request(self):
        """
        Happy path: a request is made and a response received.
        """
        correlation_id = b"corr"
        client_request = b"api " + correlation_id + b"corr more stuff"

        d = self.protocol.request(client_request)
        self.assertNoResult(d)

        # The request was written to the server.
        server_request = self.transport.getOutBuffer()
        self.assertEqual(b"\0\0\0\x17" + client_request, server_request)

        self.transport.bufferReceived(b"\0\0\0\x05" + correlation_id + b"y")
        self.assertEqual(correlation_id + b"y", self.successResultOf(d))

    def test_disconnected(self):
        """
        Pending and future requests fail when the connection goes away.
        """
        d = self.protocol.request(b"api corr stuff")
        self.assertNoResult(d)

        self.transport.disconnectReason = ConnectionLost("Bye.")
        self.transport.reportDisconnect()
        self.failureResultOf(d, ConnectionLost)

        self.failureResultOf(self.protocol.request(b"api corr more"), ConnectionLost)

    def test_unknown_correlation_id(self):
        """
        A warning is logged and the connection dropped when a response with an
        unknown correlation ID is received.
        """
        events = EventLoggingObserver.createWithCleanup(self, globalLogPublisher)
        self.transport.bufferReceived(b"\0\0\0\x101234 more stuff..")
        self.assertTrue(self.transport.disconnecting)

        [event] = events
        self.assertEqual(LogLevel.warn, event["log_level"])
        self.assertEqual(self.peer, event["peer"])
        self.assertEqual(b"1234", event["correlation_id"])

    def test_oversized_response(self):
        """
        An oversized response from the server prompts disconnection.
        """
        d = self.protocol.request(b"api corr blah blah")

        self.transport.bufferReceived(b"\xff\xff\xff\xff")  # 2**32 - 1, way too large.

        self.assertTrue(self.transport.disconnecting)
        self.assertNoResult(d)  # Will fail when the disconnect completes.
