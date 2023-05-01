# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2018, 2019, 2021 Ciena Corporation
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
import uuid
from unittest.mock import ANY, Mock, call, patch

from .. import producer as aProducer
from ..common import (
    PRODUCER_ACK_NOT_REQUIRED,
    BrokerNotAvailableError,
    CancelledError,
    FailedPayloadsError,
    LeaderNotAvailableError,
    NoResponseError,
    NotLeaderForPartitionError,
    OffsetOutOfRangeError,
    ProduceRequest,
    ProduceResponse,
    UnknownTopicOrPartitionError,
    UnsupportedCodecError,
)
from ..kafkacodec import create_message_set
from ..producer import Producer
from .testutil import make_send_requests, random_string
from twisted.internet.defer import CancelledError as tid_CancelledError
from twisted.internet.defer import Deferred, fail, succeed
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure
from twisted.test.proto_helpers import MemoryReactorClock
from twisted.trial import unittest

log = logging.getLogger(__name__)


class ProducerSendMessagesValidationTests(unittest.SynchronousTestCase):
    """
    Test the validation `afkak.producer.Producer.send_messages()` applies to
    its arguments.

    :ivar producer: `Producer` with default arguments.
    """

    def setUp(self):
        client = Mock(reactor=MemoryReactorClock())
        self.producer = Producer(client)
        self.addCleanup(self.producer.stop)

    def test_topic_type(self):
        """
        `TypeError` results when the *topic* argument is not text.
        """
        self.failureResultOf(self.producer.send_messages(1234, msgs=[b""]), TypeError)

    def test_topic_bytes(self):
        """
        `TypeError` results when the *topic* argument is a `bytes`.
        """
        self.failureResultOf(self.producer.send_messages(b"topic", msgs=[b""]), TypeError)

    def test_empty_messages(self):
        """
        `ValueError` results when the *msgs* argument is not passed or is
        empty.
        """
        self.failureResultOf(self.producer.send_messages("topic"), ValueError)
        self.failureResultOf(self.producer.send_messages("topic", msgs=[]), ValueError)

    def test_message_type(self):
        """
        `TypeError` results when members of the *msgs* sequence are not
        `bytes` or ``None``.
        """
        self.failureResultOf(self.producer.send_messages("topic", msgs=[1, 2, 3]), TypeError)
        self.failureResultOf(self.producer.send_messages("topic", msgs=["asdf"]), TypeError)

    def test_none_message(self):
        """
        A message may be ``None``. This doesn't make much sense unless there is
        also a key.
        """
        d = self.producer.send_messages("topic", key=b"key", msgs=[None])
        d.addErrback(lambda f: None)  # Handle the cancellation failure from producer.stop().

        self.assertNoResult(d)

    def test_key_type(self):
        """
        The key must not be unicode, but bytes.
        """
        self.failureResultOf(self.producer.send_messages("topic", key="key", msgs=[b"msg"]), TypeError)


class TestAfkakProducer(unittest.TestCase):
    _messages = {}
    topic = None

    def msgs(self, iterable):
        return [self.msg(x) for x in iterable]

    def msg(self, s):
        if s not in self._messages:
            self._messages[s] = b"%s-%s-%s" % (
                str(s).encode("utf-8"),
                self.id().encode("ascii"),
                str(uuid.uuid4()).encode("ascii"),
            )
        return self._messages[s]

    def setUp(self):
        super(TestAfkakProducer, self).setUp()
        if not self.topic:
            self.topic = "{}-{}".format(
                self.id()[self.id().rindex(".") + 1 :],
                random_string(10),
            )

    def test_producer_init_simplest(self):
        producer = Producer(Mock())
        self.assertEqual(
            producer.__repr__(),
            "<Producer <class 'afkak.partitioner.RoundRobinPartitioner'>:" "Unbatched:1:1000>",
        )
        producer.stop()

    def test_producer_init_batch(self):
        producer = Producer(Mock(reactor=MemoryReactorClock()), batch_send=True)
        looper = producer._sendLooper
        self.assertEqual(type(looper), LoopingCall)
        self.assertTrue(looper.running)
        producer.stop()
        self.assertFalse(looper.running)
        self.assertEqual(
            producer.__repr__(),
            "<Producer <class 'afkak.partitioner.RoundRobinPartitioner'>:" "10cnt/32768bytes/30secs:1:1000>",
        )

    def test_producer_bad_codec_value(self):
        with self.assertRaises(UnsupportedCodecError):
            Producer(Mock(), codec=99)

    def test_producer_bad_codec_type(self):
        with self.assertRaises(TypeError):
            Producer(Mock(), codec="bogus")

    def test_producer_send_messages(self):
        first_part = 23
        client = Mock(reactor=MemoryReactorClock())
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout, fail_on_error=False
        )
        # Check results when "response" fires
        self.assertNoResult(d)
        resp = [ProduceResponse(self.topic, first_part, 0, 10)]
        ret.callback(resp)
        result = self.successResultOf(d)
        self.assertEqual(result, resp[0])
        producer.stop()

    def test_producer_send_messages_keyed(self):
        """
        Test that messages sent with a key are actually sent with that key
        """
        first_part = 43
        second_part = 56
        client = Mock(reactor=MemoryReactorClock())
        ret1 = Deferred()
        client.send_produce_request.side_effect = [ret1]
        client.topic_partitions = {self.topic: [first_part, second_part, 102]}
        client.metadata_error_for_topic.return_value = False
        msgs1 = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("three"), self.msg("four")]
        key1 = b"35"
        key2 = b"foo"
        ack_timeout = 5

        # Even though we're sending keyed messages, we use the default
        # round-robin partitioner, since the requests are easier to predict
        producer = Producer(client, ack_timeout=ack_timeout, batch_send=True, batch_every_n=4)
        d1 = producer.send_messages(self.topic, key=key1, msgs=msgs1)
        d2 = producer.send_messages(self.topic, key=key2, msgs=msgs2)
        # Check the expected request was sent
        msgSet1 = create_message_set(make_send_requests(msgs1, key=key1), producer.codec)
        msgSet2 = create_message_set(make_send_requests(msgs2, key=key2), producer.codec)
        req1 = ProduceRequest(self.topic, first_part, msgSet1)
        req2 = ProduceRequest(self.topic, second_part, msgSet2)
        # Annoying, but order of requests is indeterminate...
        client.send_produce_request.assert_called_once_with(
            ANY, acks=producer.req_acks, timeout=ack_timeout, fail_on_error=False
        )
        self.assertEqual(sorted([req1, req2]), sorted(client.send_produce_request.call_args[0][0]))
        # Check results when "response" fires
        self.assertNoResult(d1)
        self.assertNoResult(d2)
        resp = [
            ProduceResponse(self.topic, first_part, 0, 10),
            ProduceResponse(self.topic, second_part, 0, 23),
        ]
        ret1.callback(resp)
        result = self.successResultOf(d1)
        self.assertEqual(result, resp[0])
        result = self.successResultOf(d2)
        self.assertEqual(result, resp[1])
        producer.stop()

    def test_producer_send_messages_keyed_same_partition(self):
        """test_producer_send_messages_keyed_same_partition
        Test that messages sent with a key are actually sent with that key,
        even if they go to the same topic/partition (batching preserves keys)
        """
        first_part = 43
        second_part = 55
        client = Mock(reactor=MemoryReactorClock())
        ret1 = Deferred()
        client.send_produce_request.side_effect = [ret1]
        client.topic_partitions = {self.topic: [first_part, second_part]}
        client.metadata_error_for_topic.return_value = False
        msgs1 = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("odd_man_out")]
        msgs3 = [self.msg("three"), self.msg("four")]
        key1 = b"99"
        key3 = b"foo"
        ack_timeout = 5

        # Even though we're sending keyed messages, we use the default
        # round-robin partitioner, since the requests are easier to predict
        producer = Producer(client, ack_timeout=ack_timeout, batch_send=True, batch_every_n=4)
        d1 = producer.send_messages(self.topic, key=key1, msgs=msgs1)
        d2 = producer.send_messages(self.topic, msgs=msgs2)
        d3 = producer.send_messages(self.topic, key=key3, msgs=msgs3)
        # Check the expected request was sent
        msgSet1 = create_message_set(
            [
                make_send_requests(msgs1, key=key1)[0],
                make_send_requests(msgs3, key=key3)[0],
            ],
            producer.codec,
        )
        msgSet2 = create_message_set(make_send_requests(msgs2), producer.codec)
        req1 = ProduceRequest(self.topic, first_part, msgSet1)
        req2 = ProduceRequest(self.topic, second_part, msgSet2)
        # Annoying, but order of requests is indeterminate...
        client.send_produce_request.assert_called_once_with(
            ANY, acks=producer.req_acks, timeout=ack_timeout, fail_on_error=False
        )
        self.assertEqual(sorted([req1, req2]), sorted(client.send_produce_request.call_args[0][0]))
        # Check results when "response" fires
        self.assertNoResult(d1)
        self.assertNoResult(d2)
        self.assertNoResult(d3)
        resp = [
            ProduceResponse(self.topic, first_part, 0, 10),
            ProduceResponse(self.topic, second_part, 0, 23),
        ]
        ret1.callback(resp)
        result = self.successResultOf(d1)
        self.assertEqual(result, resp[0])
        result = self.successResultOf(d2)
        self.assertEqual(result, resp[1])
        result = self.successResultOf(d3)
        self.assertEqual(result, resp[0])
        producer.stop()

    def test_producer_send_messages_no_acks(self):
        first_part = 19
        client = Mock(reactor=MemoryReactorClock())
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout, req_acks=PRODUCER_ACK_NOT_REQUIRED)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout, fail_on_error=False
        )
        # Check results when "response" fires
        self.assertNoResult(d)
        ret.callback([])
        result = self.successResultOf(d)
        self.assertEqual(result, None)
        producer.stop()

    def test_producer_send_messages_no_retry_fail(self):
        client = Mock(reactor=MemoryReactorClock())
        f = Failure(BrokerNotAvailableError())
        client.send_produce_request.side_effect = [fail(f)]
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]

        producer = Producer(client, max_req_attempts=1)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, 0, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req],
            acks=producer.req_acks,
            timeout=producer.ack_timeout,
            fail_on_error=False,
        )
        self.failureResultOf(d, BrokerNotAvailableError)

        producer.stop()

    def test_producer_send_messages_unexpected_err(self):
        client = Mock(reactor=MemoryReactorClock())
        f = Failure(TypeError())
        client.send_produce_request.side_effect = [fail(f)]
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]

        producer = Producer(client)
        # FIXME: Don't use patch to test logging
        with patch.object(aProducer, "log") as klog:
            d = producer.send_messages(self.topic, msgs=msgs)
            klog.error.assert_called_once_with("Unexpected failure: %r in _handle_send_response", f)
        self.failureResultOf(d, TypeError)

        producer.stop()

    def test_producer_send_messages_None_for_null_msg(self):
        first_part = 23
        client = Mock(reactor=MemoryReactorClock())
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), None, self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout, fail_on_error=False
        )
        # Check results when "response" fires
        self.assertNoResult(d)
        resp = [ProduceResponse(self.topic, first_part, 0, 10)]
        ret.callback(resp)
        result = self.successResultOf(d)
        self.assertEqual(result, resp[0])
        producer.stop()

    def test_producer_complete_batch_send_unexpected_error(self):
        # Purely for coverage
        client = Mock(reactor=MemoryReactorClock())
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        e = ValueError("test_producer_complete_batch_send_unexpected_error")
        client.send_produce_request.side_effect = e
        msgs = [self.msg("one"), self.msg("two")]

        producer = Producer(client)
        # FIXME: Don't use patch to test logging
        with patch.object(aProducer, "log") as klog:
            producer.send_messages(self.topic, msgs=msgs)
            # The error 'e' gets wrapped in a failure with a traceback, so
            # we can't easily match the call exactly...
            klog.error.assert_called_once_with("Failure detected in _complete_batch_send: %r", ANY, exc_info=ANY)

        producer.stop()

    def test_producer_send_messages_batched(self):
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        f = Failure(BrokerNotAvailableError())
        ret = [fail(f), succeed([ProduceResponse(self.topic, 0, 0, 10)])]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        batch_n = 2

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, ANY, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req],
            acks=producer.req_acks,
            timeout=producer.ack_timeout,
            fail_on_error=False,
        )
        # At first, there's no result. Have to retry due to first failure
        self.assertNoResult(d)
        clock.advance(producer._retry_interval)
        self.successResultOf(d)

        producer.stop()

    def test_producer_send_messages_batched_partial_success(self):
        """
        This tests the complexity of the error handling for a single batch
        request.

        Scenario: The producer's caller sends 5 requests to two (total) topics
                  The client's metadata is such that the producer will produce
                    requests to post msgs to 5 separate topic/partition tuples
                  The batch size is reached, so the producer sends the request
                  The caller then cancels one of the requests
                  The (mock) client returns partial success in the form of a
                    FailedPayloadsError.
                  The Producer then should return the successful results and
                    retry the failed.
                  The (mock) client then "succeeds" the remaining results.
        """
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        topic2 = "tpsmbps_two"
        client.topic_partitions = {self.topic: [0, 1, 2, 3], topic2: [4, 5, 6]}
        client.metadata_error_for_topic.return_value = False

        init_resp = [
            ProduceResponse(self.topic, 0, 0, 10),
            ProduceResponse(self.topic, 1, 6, 20),
            ProduceResponse(topic2, 5, 0, 30),
        ]
        next_resp = [
            ProduceResponse(self.topic, 2, 0, 10),
            ProduceResponse(self.topic, 1, 0, 20),
            ProduceResponse(topic2, 4, 0, 30),
        ]
        failed_payloads = [
            (ProduceRequest(self.topic, ANY, ANY), NotLeaderForPartitionError()),
            (ProduceRequest(topic2, ANY, ANY), BrokerNotAvailableError()),
        ]

        client.send_produce_request.side_effect = [
            fail(Failure(FailedPayloadsError(init_resp, failed_payloads))),
            succeed(next_resp),
        ]

        msgs = self.msgs(range(10))
        results = []

        producer = Producer(client, batch_send=True, batch_every_t=0)
        # Send 5 total requests: 4 here, one after we make sure we didn't
        # send early
        results.append(producer.send_messages(self.topic, msgs=msgs[0:3]))
        results.append(producer.send_messages(topic2, msgs=msgs[3:5]))
        results.append(producer.send_messages(self.topic, msgs=msgs[5:8]))
        results.append(producer.send_messages(topic2, msgs=msgs[8:9]))
        # No call yet, not enough messages
        self.assertFalse(client.send_produce_request.called)
        # Enough messages to start the request
        client.reset_topic_metadata.reset_mock()
        results.append(producer.send_messages(self.topic, msgs=msgs[9:10]))
        # Before the retry, there should be some results
        self.assertEqual(init_resp[0], self.successResultOf(results[0]))
        self.assertEqual(init_resp[2], self.successResultOf(results[3]))
        # And the errors should have forced a metadata reset on one of the topics.
        client.reset_topic_metadata.assert_called_with(self.topic)
        # Advance the clock to trigger retries.
        clock.advance(producer._retry_interval)
        # Check the otehr results came in
        self.assertEqual(next_resp[0], self.successResultOf(results[4]))
        self.assertEqual(next_resp[1], self.successResultOf(results[2]))
        self.assertEqual(next_resp[2], self.successResultOf(results[1]))

        producer.stop()

    def test_producer_send_messages_batched_fail(self):
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        ret = [Deferred(), Deferred(), Deferred()]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        batch_t = 5

        producer = Producer(client, batch_every_t=batch_t, batch_send=True, max_req_attempts=3)
        # Advance the clock to ensure when no messages to send no error
        clock.advance(batch_t)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check no request was yet sent
        self.assertFalse(client.send_produce_request.called)
        # Advance the clock
        clock.advance(batch_t)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, 0, msgSet)
        produce_request_call = call(
            [req],
            acks=producer.req_acks,
            timeout=producer.ack_timeout,
            fail_on_error=False,
        )
        produce_request_calls = [produce_request_call]
        client.send_produce_request.assert_has_calls(produce_request_calls)
        self.assertNoResult(d)
        # Fire the failure from the first request to the client
        ret[0].errback(OffsetOutOfRangeError("test_producer_send_messages_batched_fail"))
        # Still no result, producer should retry first
        self.assertNoResult(d)
        # Check retry wasn't immediate
        self.assertEqual(client.send_produce_request.call_count, 1)
        # Advance the clock by the retry delay
        clock.advance(producer._retry_interval)
        # Check 2nd send_produce_request (1st retry) was sent
        produce_request_calls.append(produce_request_call)
        client.send_produce_request.assert_has_calls(produce_request_calls)
        # Fire the failure from the 2nd request to the client
        ret[1].errback(BrokerNotAvailableError("test_producer_send_messages_batched_fail_2"))
        # Still no result, producer should retry one more time
        self.assertNoResult(d)
        # Advance the clock by the retry delay
        clock.advance(producer._retry_interval * 1.1)
        # Check 3nd send_produce_request (2st retry) was sent
        produce_request_calls.append(produce_request_call)
        client.send_produce_request.assert_has_calls(produce_request_calls)
        # Fire the failure from the 2nd request to the client
        ret[2].errback(LeaderNotAvailableError("test_producer_send_messages_batched_fail_3"))

        self.failureResultOf(d, LeaderNotAvailableError)

        producer.stop()

    def test_producer_cancel_request_in_batch(self):
        # Test cancelling a request before it's begun to be processed
        client = Mock(reactor=MemoryReactorClock())
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("three"), self.msg("four")]
        batch_n = 3

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d1 = producer.send_messages(self.topic, msgs=msgs)
        # Check that no request was sent
        self.assertFalse(client.send_produce_request.called)
        d1.cancel()
        self.failureResultOf(d1, CancelledError)
        d2 = producer.send_messages(self.topic, msgs=msgs2)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)
        self.assertNoResult(d2)

        producer.stop()

    def test_producer_cancel_request_in_batch_None_for_null_msg(self):
        # Test cancelling a request before it's begun to be processed
        client = Mock(reactor=MemoryReactorClock())
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("three"), self.msg("four")]
        batch_n = 3

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d1 = producer.send_messages(self.topic, msgs=msgs)
        # Check that no request was sent
        self.assertFalse(client.send_produce_request.called)
        d1.cancel()
        self.failureResultOf(d1, CancelledError)
        d2 = producer.send_messages(self.topic, msgs=msgs2)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)
        self.assertNoResult(d2)

        producer.stop()

    def test_producer_cancel_getting_topic(self):
        # Test cancelling while waiting to retry getting metadata
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        client.topic_partitions = {}  # start with no metadata
        rets = [Deferred(), Deferred()]
        client.load_metadata_for_topics.side_effect = rets
        msgs = [self.msg("one"), self.msg("two")]

        producer = Producer(client)
        d1 = producer.send_messages(self.topic, msgs=msgs)
        # Check that no request was sent
        self.assertFalse(client.send_produce_request.called)
        # Fire the result of load_metadata_for_topics, but
        # metadata_error_for_topic is still True, so it'll retry after delay
        # Advance the clock, some, but not enough to retry
        rets[0].callback(None)
        # Advance to partway thru the delay
        clock.advance(producer._retry_interval / 2)

        # Cancel the request and ake sure we got the CancelledError
        d1.cancel()
        self.failureResultOf(d1, CancelledError)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)

        # Setup the client's topics and trigger the metadata deferred
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        rets[1].callback(None)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)
        # Advance the clock again to complete the delay
        clock.advance(producer._retry_interval)
        # Make sure the retry got reset
        self.assertEqual(producer._retry_interval, producer._init_retry_interval)
        producer.stop()

    def test_producer_cancel_one_request_getting_topic(self):
        # Test cancelling a request after it's begun to be processed
        client = Mock(reactor=MemoryReactorClock())
        client.topic_partitions = {}
        ret = Deferred()
        client.load_metadata_for_topics.return_value = ret
        msgs = [self.msg("one"), self.msg("two")]
        msgs2 = [self.msg("three"), self.msg("four")]
        batch_n = 4

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d1 = producer.send_messages(self.topic, msgs=msgs)
        # Check that no request was sent
        self.assertFalse(client.send_produce_request.called)
        # This will trigger the metadata lookup
        d2 = producer.send_messages(self.topic, msgs=msgs2)
        d1.cancel()
        self.failureResultOf(d1, CancelledError)
        # Check that still no request was sent
        self.assertFalse(client.send_produce_request.called)
        self.assertNoResult(d2)
        # Setup the client's topics and trigger the metadata deferred
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        ret.callback(None)
        # Expect that only the msgs2 messages were sent
        msgSet = create_message_set(make_send_requests(msgs2), producer.codec)
        req = ProduceRequest(self.topic, 1, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req],
            acks=producer.req_acks,
            timeout=producer.ack_timeout,
            fail_on_error=False,
        )

        producer.stop()

    def test_producer_stop_during_request(self):
        """
        Test stopping producer while it's waiting for reply from client
        """
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        f = Failure(BrokerNotAvailableError())
        ret = [fail(f), Deferred()]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        batch_n = 2

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d = producer.send_messages(self.topic, msgs=msgs)
        # At first, there's no result. Have to retry due to first failure
        self.assertNoResult(d)
        clock.advance(producer._retry_interval)

        producer.stop()
        self.failureResultOf(d, tid_CancelledError)

    def test_producer_stop_waiting_to_retry(self):
        """
        Test stopping producer while it's waiting to retry a request
        """
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        f = Failure(BrokerNotAvailableError())
        ret = [fail(f)]
        client.send_produce_request.side_effect = ret
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        batch_n = 2

        producer = Producer(client, batch_every_n=batch_n, batch_send=True)
        d = producer.send_messages(self.topic, msgs=msgs)
        # At first, there's no result. Have to retry due to first failure
        self.assertNoResult(d)
        # Advance the clock, some, but not enough to retry
        clock.advance(producer._retry_interval / 2)
        # Stop the producer before the retry
        producer.stop()
        self.failureResultOf(d, tid_CancelledError)

    def test_producer_send_messages_unknown_topic(self):
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        ds = [Deferred() for _ in range(Producer.DEFAULT_REQ_ATTEMPTS)]
        client.load_metadata_for_topics.side_effect = ds
        client.metadata_error_for_topic.return_value = 3
        client.topic_partitions = {}
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # d is waiting on result from ds[0] for load_metadata_for_topics
        self.assertNoResult(d)

        # fire it with client still reporting no metadata for topic
        # The producer will retry the lookup DEFAULT_REQ_ATTEMPTS times...
        for i in range(Producer.DEFAULT_REQ_ATTEMPTS):
            ds[i].callback(None)
            # And then wait producer._retry_interval for a call back...
            clock.advance(producer._retry_interval + 0.01)
        self.failureResultOf(d, UnknownTopicOrPartitionError)
        self.assertFalse(client.send_produce_request.called)

        producer.stop()

    def test_producer_send_messages_bad_response(self):
        first_part = 68
        client = Mock(reactor=MemoryReactorClock())
        ret = Deferred()
        client.send_produce_request.return_value = ret
        client.topic_partitions = {self.topic: [first_part, 101, 102, 103]}
        client.metadata_error_for_topic.return_value = False
        msgs = [self.msg("one"), self.msg("two")]
        ack_timeout = 5

        producer = Producer(client, ack_timeout=ack_timeout)
        d = producer.send_messages(self.topic, msgs=msgs)
        # Check the expected request was sent
        msgSet = create_message_set(make_send_requests(msgs), producer.codec)
        req = ProduceRequest(self.topic, first_part, msgSet)
        client.send_produce_request.assert_called_once_with(
            [req], acks=producer.req_acks, timeout=ack_timeout, fail_on_error=False
        )
        # Check results when "response" fires
        self.assertNoResult(d)
        ret.callback([])
        self.failureResultOf(d, NoResponseError)
        producer.stop()

    def test_producer_send_timer_failed(self):
        """
        Test that the looping call is restarted when _send_batch errs
        Somewhat artificial test to confirm that when failures occur in
        _send_batch (which cause the looping call to terminate) that the
        looping call is restarted.
        """
        clock = MemoryReactorClock()
        client = Mock(reactor=clock)
        client.topic_partitions = {self.topic: [0, 1, 2, 3]}
        client.metadata_error_for_topic.return_value = False
        batch_t = 5

        # FIXME: Don't use patch to test logging
        with patch.object(aProducer, "log") as klog:
            producer = Producer(client, batch_send=True, batch_every_t=batch_t)
            msgs = [self.msg("one"), self.msg("two")]
            d = producer.send_messages(self.topic, msgs=msgs)
            # Check no request was yet sent
            self.assertFalse(client.send_produce_request.called)
            # Patch Producer's Deferred to throw an exception
            with patch.object(aProducer, "Deferred") as d:
                d.side_effect = ValueError("test_producer_send_timer_failed induced failure")
                # Advance the clock
                clock.advance(batch_t)
            # Check the expected message was logged by the looping call restart
            klog.warning.assert_called_once_with(
                "Batch timer failed: %s. Will restart.",
                ANY,
                exc_info=ANY,
            )
        # Check that the looping call was restarted
        self.assertTrue(producer._sendLooper.running)

        producer.stop()

    def test_producer_non_integral_batch_every_n(self):
        client = Mock(reactor=MemoryReactorClock())
        with self.assertRaises(TypeError):
            producer = Producer(client, batch_send=True, batch_every_n="10")
            producer.__repr__()  # pragma: no cover  # STFU pyflakes

    def test_producer_non_integral_batch_every_b(self):
        client = Mock(reactor=MemoryReactorClock())
        with self.assertRaises(TypeError):
            producer = Producer(client, batch_send=True, batch_every_b="10")
            producer.__repr__()  # pragma: no cover  # STFU pyflakes
