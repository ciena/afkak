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

from twisted.internet.defer import Deferred, fail
from twisted.internet.protocol import Factory, connectionDone
from twisted.logger import Logger
from twisted.protocols.basic import Int32StringReceiver

"""
Twisted protocols that understand Kafka message framing
"""

log = logging.getLogger("afkak.protocol")
log.addHandler(logging.NullHandler())


class _BaseKafkaProtocol(Int32StringReceiver):
    MAX_LENGTH = 2**31 - 1  # Max a signed Int32 can represent


class KafkaProtocol(_BaseKafkaProtocol):
    """
    Very thin wrapper around the Int32StringReceiver
    Simply knows to call its factory.handleResponse()
    method with the string received by stringReceived() and
    to cleanup the factory reference when the connection is lost
    """

    factory = None

    def stringReceived(self, string):
        self.factory.handleResponse(string)

    def connectionLost(self, reason=connectionDone):
        self.factory._connectionLost(reason)
        self.factory = None

    def lengthLimitExceeded(self, length):
        log.error(
            "Broker at %s sent a %d byte message, exceeding the size limit of %d. " "Terminating connection.",
            self.transport.getPeer(),
            length,
            self.MAX_LENGTH,
        )
        self.transport.loseConnection()


class KafkaBootstrapProtocol(_BaseKafkaProtocol):
    """
    `KafkaBootstrapProtocol` sends and receives Kafka messages.

    It knows just enough about Kafka message framing to correlate responses
    with requests. A deferred is issued for every request and fires when
    a response is received or the connection is lost.

    :ivar dict _pending:
        Map of correlation ID to Deferred.
    """

    _log = Logger()

    def connectionMade(self):
        self._pending = {}
        self._failed = None

    def stringReceived(self, response):
        """
        Handle a response from the broker.
        """
        correlation_id = response[0:4]
        try:
            d = self._pending.pop(correlation_id)
        except KeyError:
            self._log.warn(
                ("Response has unknown correlation ID {correlation_id!r}." " Dropping connection to {peer}."),
                correlation_id=correlation_id,
                peer=self.transport.getPeer(),
            )
            self.transport.loseConnection()
        else:
            d.callback(response)

    def connectionLost(self, reason=connectionDone):
        """
        Mark the protocol as failed and fail all pending operations.
        """
        self._failed = reason
        pending, self._pending = self._pending, None
        for d in pending.values():
            d.errback(reason)

    def lengthLimitExceeded(self, length):
        self._log.error(
            "Broker at {peer} sent a {length:,d} byte message, exceeding the size limit of {max_length:,d}.",
            peer=self.transport.getPeer(),
            length=length,
            max_length=self.MAX_LENGTH,
        )
        self.transport.loseConnection()

    def request(self, request):
        """
        Send a request to the Kafka broker.

        :param bytes request:
            The bytes of a Kafka `RequestMessage`_ structure. It must have
            a unique (to this connection) correlation ID.

        :returns:
            `Deferred` which will:

              - Succeed with the bytes of a Kafka `ResponseMessage`_
              - Fail when the connection terminates

        .. _RequestMessage:: https://kafka.apache.org/protocol.html#protocol_messages

        """
        if self._failed is not None:
            return fail(self._failed)
        correlation_id = request[4:8]
        assert correlation_id not in self._pending
        d = Deferred()
        self.sendString(request)
        self._pending[correlation_id] = d
        return d


bootstrapFactory = Factory.forProtocol(KafkaBootstrapProtocol)
