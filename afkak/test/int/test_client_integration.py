# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
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

import logging
import random

from afkak.common import FetchRequest, OffsetCommitRequest, OffsetFetchRequest, OffsetRequest, ProduceRequest
from afkak.kafkacodec import create_message
from afkak.test.testutil import random_string

from .intutil import IntegrationMixin, kafka_versions
from twisted.internet.defer import inlineCallbacks
from twisted.trial import unittest

log = logging.getLogger(__name__)


class TestAfkakClientIntegration(IntegrationMixin, unittest.TestCase):
    harness_kw = dict(
        replicas=3,
        partitions=2,
        message_max_bytes=12 * 1048576,  # 12 MB
    )
    client_kw = dict(
        timeout=30000,  # Large timeout because Travis is slow.
    )

    @kafka_versions("all")
    @inlineCallbacks
    def test_consume_none(self):
        fetch = FetchRequest(self.topic, 0, 0, 1024)

        [fetch_resp] = yield self.retry_while_broker_errors(
            self.client.send_fetch_request,
            [fetch],
            max_wait_time=1000,
        )
        self.assertEqual(fetch_resp.error, 0)
        self.assertEqual(fetch_resp.topic, self.topic)
        self.assertEqual(fetch_resp.partition, 0)

        messages = list(fetch_resp.messages)
        self.assertEqual(len(messages), 0)

    @kafka_versions("all")
    @inlineCallbacks
    def test_produce_request(self):
        produce = ProduceRequest(
            self.topic,
            0,
            [create_message(self.topic.encode() + b" message %d" % i) for i in range(5)],
        )

        [produce_resp] = yield self.retry_while_broker_errors(
            self.client.send_produce_request,
            [produce],
        )
        self.assertEqual(produce_resp.error, 0)
        self.assertEqual(produce_resp.topic, self.topic)
        self.assertEqual(produce_resp.partition, 0)
        self.assertEqual(produce_resp.offset, 0)

    @kafka_versions("all")
    @inlineCallbacks
    def test_produce_large_request(self):
        """
        Send large messages of about 950 KB in size. Note that per the default
        configuration Kafka only allows up to 1 MiB messages.
        """
        produce = ProduceRequest(
            self.topic,
            0,
            [
                create_message(self.topic.encode() + b" message %d: " % i + b"0123456789" * (950 * 100))
                for i in range(5)
            ],
        )

        [produce_resp] = yield self.retry_while_broker_errors(self.client.send_produce_request, [produce])
        self.assertEqual(produce_resp.error, 0)
        self.assertEqual(produce_resp.topic, self.topic)
        self.assertEqual(produce_resp.partition, 0)
        self.assertEqual(produce_resp.offset, 0)

    @kafka_versions("all")
    @inlineCallbacks
    def test_roundtrip_large_request(self):
        """
        A large request can be produced and fetched.
        """
        log.debug("Timestamp Before ProduceRequest")
        # Single message of a bit less than 1 MiB
        message = create_message(self.topic.encode() + b" message 0: " + (b"0123456789" * 10 + b"\n") * 90)
        produce = ProduceRequest(self.topic, 0, [message])
        log.debug("Timestamp before send")
        [produce_resp] = yield self.retry_while_broker_errors(self.client.send_produce_request, [produce])
        log.debug("Timestamp after send")
        self.assertEqual(produce_resp.error, 0)
        self.assertEqual(produce_resp.topic, self.topic)
        self.assertEqual(produce_resp.partition, 0)
        self.assertEqual(produce_resp.offset, 0)

        log.debug("Sending FetchRequest")
        # Fetch request with max size of 1 MiB
        fetch = FetchRequest(self.topic, 0, 0, 1024**2)
        [fetch_resp] = yield self.client.send_fetch_request([fetch], max_wait_time=10000)
        log.debug("Got FetchResponse %r", fetch_resp)
        self.assertEqual(fetch_resp.error, 0)
        self.assertEqual(fetch_resp.topic, self.topic)
        self.assertEqual(fetch_resp.partition, 0)

        messages = list(fetch_resp.messages)
        self.assertEqual(len(messages), 1)

    ####################
    #   Offset Tests   #
    ####################

    @kafka_versions("all")
    @inlineCallbacks
    def test_send_offset_request(self):
        req = OffsetRequest(self.topic, 0, -1, 100)
        [resp] = yield self.retry_while_broker_errors(self.client.send_offset_request, [req])
        self.assertEqual(resp.error, 0)
        self.assertEqual(resp.topic, self.topic)
        self.assertEqual(resp.partition, 0)
        self.assertEqual(resp.offsets, (0,))

    @kafka_versions("all")
    @inlineCallbacks
    def test_commit_fetch_offsets(self):
        """
        Commit offsets, then fetch them to verify that the commit succeeded.
        """
        # RANT: https://cwiki.apache.org/confluence/display/KAFKA/A+Guide+To+The+Kafka+Protocol
        # implies that the metadata supplied with the commit will be returned by
        # the fetch, but under 0.8.2.1 with a API_version of 0, it's not. Switch
        # to using the V1 API and it works.
        c_group = "CG_1"
        metadata = "My_Metadata_{}".format(random_string(10)).encode("ascii")
        offset = random.randint(0, 1024)
        log.debug(
            "Committing offset: %d metadata: %s for topic: %s part: 0",
            offset,
            metadata,
            self.topic,
        )
        req = OffsetCommitRequest(self.topic, 0, offset, -1, metadata)
        # We have to retry, since the client doesn't, and Kafka will
        # create the topic on the fly, but the first request will fail
        [resp] = yield self.retry_while_broker_errors(self.client.send_offset_commit_request, c_group, [req])
        self.assertEqual(getattr(resp, "error", -1), 0)

        req = OffsetFetchRequest(self.topic, 0)
        [resp] = yield self.client.send_offset_fetch_request(c_group, [req])
        self.assertEqual(resp.error, 0)
        self.assertEqual(resp.offset, offset)
        # Check we received the proper metadata in the response
        self.assertEqual(resp.metadata, metadata)
        log.debug("test_commit_fetch_offsets: Test Complete.")
