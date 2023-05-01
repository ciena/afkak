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

import logging
import sys
from unittest.mock import ANY, Mock, call, patch

from .. import consumer as kconsumer  # for patching
from ..common import (
    KAFKA_SUCCESS,
    OFFSET_COMMITTED,
    OFFSET_EARLIEST,
    OFFSET_LATEST,
    TIMESTAMP_INVALID,
    ConsumerFetchSizeTooSmall,
    FetchRequest,
    FetchResponse,
    InvalidConsumerGroupError,
    KafkaUnavailableError,
    Message,
    OffsetCommitRequest,
    OffsetCommitResponse,
    OffsetFetchRequest,
    OffsetFetchResponse,
    OffsetOutOfRangeError,
    OffsetRequest,
    OffsetResponse,
    OperationInProgress,
    RestartError,
    RestopError,
    SourcedMessage,
    UnknownError,
)
from ..consumer import FETCH_BUFFER_SIZE_BYTES, Consumer
from ..kafkacodec import KafkaCodec, create_message
from .logtools import capture_logging
from twisted.internet.defer import CancelledError, Deferred, fail, succeed
from twisted.python.failure import Failure
from twisted.test.proto_helpers import MemoryReactorClock
from twisted.trial import unittest

log = logging.getLogger(__name__)


class TestAfkakConsumer(unittest.SynchronousTestCase):
    maxDiff = None

    def test_consumer_non_integer_partitions(self):
        with self.assertRaises(ValueError):
            Consumer(Mock(), "topic", "0", Mock())

    def test_consumer_non_integer_commit_every_n(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(reactor=MemoryReactorClock()),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_non_integer_commit_every_n",
                auto_commit_every_n=3.5,
            )

    def test_consumer_negative_commit_every_n(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(reactor=MemoryReactorClock()),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_negative_commit_every_n",
                auto_commit_every_n=-300,
            )

    def test_consumer_non_integer_commit_every_ms(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_non_integer_commit_every_ms",
                auto_commit_every_ms=3.5,
            )

    def test_consumer_negative_commit_every_ms(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_negative_commit_every_ms",
                auto_commit_every_ms=-20,
            )

    def test_consumer_non_integer_retry_max_attempts(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_non_integer_retry_max_attempts",
                request_retry_max_attempts=20.3,
            )

    def test_consumer_negative_retry_max_attempts(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_negative_retry_max_attempts",
                request_retry_max_attempts=-20,
            )

    def test_consumer_non_str_auto_offset_reset(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_non_str_auto_offset_reset",
                auto_offset_reset=111,
            )

    def test_consumer_str_not_expected(self):
        with self.assertRaises(ValueError):
            Consumer(
                Mock(),
                "topic",
                0,
                Mock(),
                consumer_group="test_consumer_str_not_expected",
                auto_offset_reset="not_expected",
            )

    def test_consumer_init(self):
        client = Mock(reactor=MemoryReactorClock())
        partition = 9
        processor = Mock()
        consumer_group = "My Consumer Group"
        consumer_metadata = b"My Commit Metadata"
        auto_commit_msgs = 24
        auto_commit_time = 60000

        Consumer(
            client,
            "tTopic",
            partition,
            processor,
            consumer_group,
            consumer_metadata,
            auto_commit_msgs,
            auto_commit_time,
            4096,
            1000,
            256 * 1024,
            8 * 1024 * 1024,
            1.0,
            30,
        )

    def test_consumer_buffer_size_err(self):
        with self.assertRaises(ValueError):
            Consumer(None, "Grues", 99, Mock(), buffer_size=8192, max_buffer_size=4096)

    def test_consumer_auto_commit_parms_err(self):
        with self.assertRaises(ValueError):
            Consumer(None, "Agnot", 500, Mock(), auto_commit_every_ms=8192)

    def test_consumer_repr(self):
        mockClient = Mock(reactor=MemoryReactorClock())
        consumer = Consumer(mockClient, "Grues", 99, lambda c, m: None)
        self.assertEqual("<Consumer Grues/99 initialized>", repr(consumer))

    def test_consumer_start_offset(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "offset22Topic", 18, Mock())
        d = consumer.start(22)
        request = FetchRequest("offset22Topic", 18, 22, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_start_earliest(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "earliestTopic", 9, Mock())
        d = consumer.start(OFFSET_EARLIEST)
        request = OffsetRequest("earliestTopic", 9, OFFSET_EARLIEST, 1)
        mockclient.send_offset_request.assert_called_once_with([request])
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_start_latest(self):
        offset = 2346  # arbitrary
        topic = "latestTopic"
        part = 10
        reqs_ds = [Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_request.return_value = reqs_ds[0]
        mockclient.send_offset_fetch_request.return_value = reqs_ds[1]
        consumer = Consumer(mockclient, topic, part, Mock())
        d = consumer.start(OFFSET_LATEST)
        # Make sure request was made
        request = OffsetRequest(topic, part, OFFSET_LATEST, 1)
        mockclient.send_offset_request.assert_called_once_with([request])
        # Deliver the responses
        responses = [OffsetResponse(topic, part, KAFKA_SUCCESS, [offset])]
        reqs_ds[0].callback(responses)
        self.assertEqual(offset, consumer._fetch_offset)
        # Check that the message fetch was started
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_start_committed(self):
        offset = 2996  # arbitrary, offset we're committing
        fetch_offset = offset + 1  # fetch at next offset after committed
        topic = "committedTopic"
        part = 23
        reqs_ds = [Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_fetch_request.return_value = reqs_ds[0]
        mockclient.send_fetch_request.return_value = reqs_ds[1]
        consumer = Consumer(mockclient, topic, part, Mock(), consumer_group="myGroup")
        d = consumer.start(OFFSET_COMMITTED)
        # Make sure request was made
        request = OffsetFetchRequest(topic, part)
        mockclient.send_offset_fetch_request.assert_called_once_with("myGroup", [request])
        # Deliver the response
        responses = [OffsetFetchResponse(topic, part, offset, b"METADATA", KAFKA_SUCCESS)]
        reqs_ds[0].callback(responses)
        self.assertEqual(fetch_offset, consumer._fetch_offset)
        # Check that the message fetch was started
        request = FetchRequest(topic, part, fetch_offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_start_committed_bad_group(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "committedTopic", 11, Mock())
        d = consumer.start(OFFSET_COMMITTED)
        self.assertFalse(mockclient.called)
        self.failureResultOf(d, InvalidConsumerGroupError)

    def test_consumer_commit_bad_group(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "committedTopic", 11, Mock())
        d = consumer.commit()
        self.assertFalse(mockclient.called)
        self.failureResultOf(d, InvalidConsumerGroupError)

    def test_consumer_commit_no_progress(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "committedTopic", 11, Mock(), "cGroup")
        d = consumer.commit()
        self.assertFalse(mockclient.called)
        self.assertEqual(self.successResultOf(d), None)

    def test_consumer_commit_with_progress(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        return_value = Deferred()
        mockclient.send_offset_commit_request.return_value = return_value
        the_group = "Band on the Run"
        the_topic = "test_consumer_commit_with_progress_topic"
        the_part = 1134
        the_offset = 4269
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs
        consumer._commit_looper = Mock()  # Mock a looping call to test reset
        d = consumer.commit()
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        consumer._commit_looper.reset.assert_called_once_with()
        self.assertNoResult(d)

    def test_consumer_commit_during_commit(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        return_value = Deferred()
        mockclient.send_offset_commit_request.return_value = return_value
        the_group = "The Cure"
        the_topic = "test_consumer_commit_during_commit_topic"
        the_part = 1
        the_offset = 28616
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs
        consumer._commit_looper = Mock()  # Mock a looping call to test reset
        d1 = consumer.commit()
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        consumer._commit_looper.reset.assert_called_once_with()
        self.assertFalse(d1.called)
        d2 = consumer.commit()
        self.failureResultOf(d2, OperationInProgress)

    def test_consumer_auto_commit_by_msgs(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        client_requests = [Deferred(), Deferred()]
        mockclient.send_fetch_request.return_value = client_requests[0]
        mockclient.send_offset_commit_request.return_value = client_requests[1]
        the_group = "Horse with no name"
        the_topic = "test_consumer_auto_commit_by_msgs"
        the_part = 1341
        the_offset = 2694
        the_processor = Mock()
        proc_deferreds = [
            Deferred(),
            Deferred(),
            Deferred(),
            Deferred(),
            Deferred(),
            Deferred(),
        ]
        the_processor.side_effect = proc_deferreds
        # Create a consumer and start it at offset 0
        consumer = Consumer(
            mockclient,
            the_topic,
            the_part,
            the_processor,
            the_group,
            auto_commit_every_n=1,
            auto_commit_every_ms=0,
        )  # No auto_commit by time
        start_d = consumer.start(the_offset)
        # Fire a response to the fetch request
        messages = [create_message(m) for m in [b"msg1", b"hi", b"boo", b"foo", b"fun"]]
        message_set = KafkaCodec._encode_message_set(messages, the_offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(the_topic, the_part, KAFKA_SUCCESS, 486, message_iter)]
        client_requests[0].callback(responses)
        # Batch of messages delivered, expect the_processor to have been called
        the_processor.assert_called_once_with(consumer, [SourcedMessage(the_topic, the_part, the_offset, messages[0])])
        # Finish the processing of the processor
        proc_deferreds[0].callback(True)
        # Expect a commit request
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        # 'Send' the commit response
        commit_response = [
            OffsetCommitResponse(the_topic, the_part, KAFKA_SUCCESS),
        ]
        client_requests[1].callback(commit_response)

        # Stop the consumer to cleanup any outstanding operations
        self.assertNoResult(start_d)
        last_processed = consumer.stop()
        self.assertEqual(self.successResultOf(start_d), the_offset)
        self.assertEqual(last_processed, the_offset)

    def test_consumer_commit_retry(self):
        mockclient = Mock(reactor=MemoryReactorClock())
        commit_ds = [fail(KafkaUnavailableError()), Deferred()]
        mockclient.send_offset_commit_request.side_effect = commit_ds
        the_group = "Sade"
        the_topic = "test_consumer_commit_retry"
        the_part = 19
        the_offset = 5431
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs
        d = consumer.commit()
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        mockclient.reactor.advance(consumer.retry_max_delay)
        the_call = call(the_group, [the_request], consumer_id="", group_generation_id=-1)
        expected_calls = [the_call, the_call]
        self.assertEqual(mockclient.send_offset_commit_request.mock_calls, expected_calls)
        commit_response = [OffsetCommitResponse(the_topic, the_part, KAFKA_SUCCESS)]
        self.assertFalse(d.called)
        commit_ds[1].callback(commit_response)
        self.assertTrue(d.called)

    def test_consumer_auto_commit_fail_errbacks_start_d(self):
        """
        A failure to auto-commit is reported on the Deferred returned by `start()`.
        """
        mockclient = Mock(reactor=MemoryReactorClock())
        the_group = "The Clash"
        the_topic = "test_consumer_auto_commit_fail_errbacks_start_d"
        the_part = 20
        the_offset = 989
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        # Make the commit fail with something that won't be retried
        the_fail = Failure(ValueError(the_topic))
        fetch_d = Deferred()
        mockclient.send_offset_commit_request.return_value = fail(the_fail)
        mockclient.send_fetch_request.return_value = fetch_d
        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs
        # Start the consumer
        start_d = consumer.start(0)
        # fake an _commit_looper call
        consumer._auto_commit()
        # Make sure it tried to commit
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        # Make sure the start_d was errback'd
        self.assertEqual(self.failureResultOf(start_d, ValueError), the_fail)
        # Clean up
        consumer.stop()

    def test_consumer_commit_retry_to_failure(self):
        """
        The consumer eventually gives up trying to commit offsets when the
        retry count is exhausted.
        """

        def make_fail(*_, **__):
            return fail(KafkaUnavailableError("commit_retry_to_failure"))

        commit_attempts = 12  # gets us two warnings
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_commit_request.side_effect = make_fail

        the_group = "TearsForFears"
        the_topic = "test_consumer_commit_retry_to_failure"
        the_part = 1
        the_offset = 4513
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)

        # Create a consumer and muck with the state a bit...
        # Also setup the retry timing to give expected retries/log calls
        consumer = Consumer(
            mockclient,
            the_topic,
            the_part,
            Mock(),
            the_group,
            request_retry_init_delay=1.20205,
            request_retry_max_delay=4.0,
            request_retry_max_attempts=commit_attempts,
        )
        consumer._last_processed_offset = the_offset  # Fake processed msgs
        commit_d = consumer.commit()
        with capture_logging(kconsumer.log) as records:
            while not commit_d.called:
                clock.advance(consumer.retry_max_delay)

        # At least one warning was logged.
        self.assertIn("WARNING", [r.levelname for r in records])

        # Make sure we retried the request the proper number of times
        the_call = call(the_group, [the_request], consumer_id="", group_generation_id=-1)
        expected_calls = [the_call] * commit_attempts
        self.assertEqual(mockclient.send_offset_commit_request.mock_calls, expected_calls)

        self.failureResultOf(commit_d, KafkaUnavailableError)

    def test_consumer_start_twice(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "twice_start", 12, Mock())
        consumer.start(0)
        self.assertRaises(RestartError, consumer.start, 0)

    def test_consumer_stop_during_offset(self):
        topic = "stop_during_offset"
        part = 101
        reqs_ds = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_request.return_value = reqs_ds[0]
        consumer = Consumer(mockclient, topic, part, Mock())
        d = consumer.start(OFFSET_LATEST)
        # Make sure request was made
        request = OffsetRequest(topic, part, OFFSET_LATEST, 1)
        mockclient.send_offset_request.assert_called_once_with([request])
        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_stop_before_fetch_response(self):
        """test_consumer_stop_before_fetch_response

        BPPF-472: Consumer would enter a tight-loop, blocking reactor by
        continuous, recursive adding of `_handle_fetch_response` from
        within it. This was due to the `self._msg_block_d` being
        cancelled, but not cleared.  This test ensures that if
        `_handle_fetch_response` is entered after the consumer has been
        stopped, that this recursive infinite loop does not occur.
        """

        def processor(consumer, messages):
            # Stop the consumer.
            consumer.stop()

        def make_response(s_id):
            # Create a response to the fetch request
            messages = [create_message("msg{}".format(n).encode("ascii")) for n in range(s_id, s_id + 4)]
            message_set = KafkaCodec._encode_message_set(messages, offset)
            message_iter = KafkaCodec._decode_message_set_iter(message_set)
            return [FetchResponse(topic, part, KAFKA_SUCCESS, 486, message_iter)]

        topic = "test_consumer_stop_before_fetch_response"
        part = 201
        offset = 44
        req_ds = [Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = req_ds
        consumer = Consumer(mockclient, topic, part, processor)
        start_d = consumer.start(offset)
        # Make sure request was made
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Fire a response to the fetch request
        req_ds[0].callback(make_response(offset))
        self.assertIsNone(self.successResultOf(start_d))
        clock.advance(consumer.retry_max_delay)
        expected_calls = [
            call(
                [request],
                max_wait_time=consumer.fetch_max_wait_time,
                min_bytes=consumer.fetch_min_bytes,
            )
        ]  # Unfixed code makes 2nd req.
        self.assertEqual(mockclient.send_fetch_request.mock_calls, expected_calls)
        req_ds[1].callback(make_response(offset + 4))  # Unfixed code hangs here

    def test_consumer_stop_during_fetch_retry(self):
        fetch_ds = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = fetch_ds
        consumer = Consumer(mockclient, "committedTopic", 11, "FakeProc", consumer_group="myGroup")
        d = consumer.start(0)
        with patch.object(kconsumer, "log") as klog:
            f = Failure(UnknownError())
            fetch_ds[0].errback(f)
            klog.debug.assert_called_once_with("%r: Failure fetching messages from kafka: %r", consumer, f)
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_offset_fetch_retry_to_failure(self):
        """
        The consumer eventually gives up trying to load offsets.
        """

        def make_fail(*_, **__):
            return fail(KafkaUnavailableError("offset_fetch_retry_to_failure"))

        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        the_processor = Mock()
        the_topic = "test_consumer_offset_fetch_retry_to_failure_topic"
        the_part = 13
        fetch_offset = OFFSET_EARLIEST
        fetch_attempts = 100
        mockclient.send_offset_request.side_effect = make_fail
        consumer = Consumer(
            mockclient,
            the_topic,
            the_part,
            the_processor,
            request_retry_max_attempts=fetch_attempts,
        )
        request = OffsetRequest(the_topic, the_part, fetch_offset, 1)

        d = consumer.start(fetch_offset)
        while not d.called:
            clock.advance(consumer.retry_max_delay)

        self.failureResultOf(d, KafkaUnavailableError)
        offset_call = call([request])
        self.assertEqual(mockclient.send_offset_request.mock_calls, [offset_call] * fetch_attempts)
        self.assertIsNone(consumer.stop())

    def test_consumer_fetch_retry_to_failure(self):
        def make_fail(*_, **__):
            return fail(KafkaUnavailableError("fetch_retry_to_failure"))

        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        the_processor = Mock()
        the_topic = "test_consumer_fetch_retry_to_failure_topic"
        the_part = 12
        fetch_offset = 0
        fetch_attempts = 100
        mockclient.send_fetch_request.side_effect = make_fail
        consumer = Consumer(
            mockclient,
            the_topic,
            the_part,
            the_processor,
            request_retry_max_attempts=fetch_attempts,
        )
        request = FetchRequest(the_topic, the_part, fetch_offset, consumer.buffer_size)

        d = consumer.start(fetch_offset)
        while not d.called:
            clock.advance(consumer.retry_max_delay)

        self.failureResultOf(d, KafkaUnavailableError)
        fetch_call = call(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        self.assertEqual(mockclient.send_fetch_request.mock_calls, [fetch_call] * fetch_attempts)
        self.assertIsNone(consumer.stop())

    def test_consumer_stop_during_initial_proc_call(self):
        # processor's deferred
        proc_d = Deferred(Mock())
        pmock_errback = Mock()
        proc_d.addErrback(pmock_errback)
        proc_called = [False]

        def processor(consumer, msglist):
            proc_called[0] = True
            consumer.stop()
            return proc_d

        topic = "proc_stop"
        part = 33
        offset = 67

        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        fetch_ds = [Deferred(), Deferred()]
        mockclient.send_fetch_request.side_effect = fetch_ds
        consumer = Consumer(mockclient, topic, part, processor)
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v9", b"k9"),
            create_message(b"v10", b"k10"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 486, message_iter)]
        fetch_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertTrue(proc_called[0])
        # Make sure the processor deferred was cancelled
        proc_d._canceller.assert_called_once_with(proc_d)
        # Make sure processor errback was called (canceller didn't callback)
        self.assertTrue(pmock_errback.called)

        # Make sure the start callback was called, and the errback wasn't
        self.assertIsNone(self.successResultOf(start_d))

    def test_consumer_stop_during_commit_retry(self):
        # setup a client which will return a message block in response to fetch
        # and just fail on the commit
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_commit_request.return_value = fail(KafkaUnavailableError())
        fetch_d = Deferred()
        mockclient.send_fetch_request.return_value = fetch_d
        the_group = "Duran Duran"
        the_topic = "test_consumer_stop_during_commit_retry"
        the_part = 11
        the_offset = 0
        the_highwater = 5
        # Create a consumer with autocommit disabled
        mock_proc = Mock()
        consumer = Consumer(
            mockclient,
            the_topic,
            the_part,
            mock_proc,
            the_group,
            auto_commit_every_n=0,
            auto_commit_every_ms=0,
        )
        # Start the consumer at offset zero
        d = consumer.start(the_offset)

        # create & deliver the response
        messages = [create_message(b"aotearoa"), create_message(b"bikini")]
        message_set = KafkaCodec._encode_message_set(messages, the_offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(the_topic, the_part, KAFKA_SUCCESS, the_highwater, message_iter)]
        fetch_d.callback(responses)

        mock_proc.assert_called_once_with(
            consumer,
            [
                SourcedMessage(the_topic, 11, 0, Message(0, 0, None, b"aotearoa")),
                SourcedMessage(the_topic, 11, 1, Message(0, 0, None, b"bikini")),
            ],
        )

        commit_d = consumer.commit()
        self.assertNoResult(commit_d)

        self.assertEqual(1, consumer.stop())
        self.assertEqual(1, self.successResultOf(d))
        # Now the commit_d should have been cancelled, check for the failure
        self.failureResultOf(commit_d, CancelledError)

    def test_consumer_stop_during_commit(self):
        # setup a client which will return a message block in response to fetch
        # and just fail on the commit
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_commit_request.return_value = Deferred()
        mockclient.send_fetch_request.return_value = Deferred()
        the_group = "U2"
        the_topic = "test_consumer_stop_during_commit"
        the_part = 11
        the_offset = 0
        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group, auto_commit_every_ms=0)
        start_d = consumer.start(the_offset)
        consumer._last_processed_offset = the_offset  # Fake processed msgs

        # Start a commit, don't fire the deferred, assert there's no result
        commit_d = consumer.commit()
        self.assertNoResult(commit_d)
        self.assertEqual(consumer._commit_ds[0], commit_d)

        # Stop the consumer, assert the start_d fired, and commit_d errbacks
        self.assertEqual(the_offset, consumer.stop())
        self.assertEqual(the_offset, self.successResultOf(start_d))
        self.failureResultOf(commit_d, CancelledError)

    def test_consumer_stop_not_started(self):
        mockclient = Mock(reactor=MemoryReactorClock())
        consumer = Consumer(mockclient, "stop_no_start", 12, Mock())
        self.assertRaises(RestopError, consumer.stop)

    def test_consumer_processor_error(self):
        reqs_ds = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        proc_d = Deferred()

        topic = "proc_error"
        part = 30
        offset = 38

        mockclient.send_fetch_request.side_effect = reqs_ds
        consumer = Consumer(mockclient, topic, part, lambda *args, **kwargs: proc_d)
        d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_d, consumer._processor_d)

        # Errback the processor deferred
        f = Failure(KeyError())  # Pick some random failure mode
        proc_d.errback(f)
        # Ensure the start() deferred was errback'd
        self.assertEqual(self.failureResultOf(d), f)
        self.assertIsNone(consumer.stop())
        self.assertIsNone(consumer.last_processed_offset)

    def test_consumer_error_during_offset(self):
        topic = "error_during_offset"
        part = 991
        reqs_ds = [Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_request.side_effect = reqs_ds
        consumer = Consumer(mockclient, topic, part, Mock())
        d = consumer.start(OFFSET_LATEST)
        # Make sure request for offset was made
        request = OffsetRequest(topic, part, OFFSET_LATEST, 1)
        mockclient.send_offset_request.assert_called_once_with([request])
        # Errback the first request
        f = Failure(KafkaUnavailableError())  # Perhaps kafka wasn't up yet...
        with patch.object(kconsumer, "log"):
            reqs_ds[0].errback(f)
        # Advance the clock to trigger the 2nd request
        clock.advance(consumer.retry_delay + 1)  # fire the callLater
        self.assertEqual(2, mockclient.send_offset_request.call_count)

        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_offset_out_of_range_error_with_auto_reset_to_earliest(self):
        topic = "offset_out_of_range_error"
        part = 911
        offset = 10000
        reqs_d = Deferred()
        mockclient = Mock()
        mockclient.send_fetch_request.return_value = reqs_d

        consumer = Consumer(mockclient, topic, part, Mock(), auto_offset_reset=OFFSET_EARLIEST)
        consumer.start(offset)

        self.assertIsNone(consumer._retry_call)
        fetch_request = FetchRequest(
            topic=topic,
            partition=part,
            offset=offset,
            max_bytes=FETCH_BUFFER_SIZE_BYTES,
        )
        consumer.client.send_fetch_request.assert_called_once_with([fetch_request], max_wait_time=100, min_bytes=65536)

        f = Failure(OffsetOutOfRangeError())
        reqs_d.errback(f)

        self.assertEqual(consumer._fetch_offset, OFFSET_EARLIEST)
        self.assertIsNotNone(consumer._retry_call)

        earliest_offset_request = OffsetRequest(topic, part, OFFSET_EARLIEST, 1)

        with patch.object(kconsumer, "log"):
            consumer._do_fetch()

        consumer.client.send_offset_request.assert_called_once_with([earliest_offset_request])

        consumer.stop()

    def test_consumer_offset_out_of_range_error_with_auto_reset_to_latest(self):
        topic = "offset_out_of_range_error"
        part = 911
        offset = 10000
        reqs_d = Deferred()
        mockclient = Mock()
        mockclient.send_fetch_request.side_effect = reqs_d
        consumer = Consumer(mockclient, topic, part, Mock(), auto_offset_reset=OFFSET_LATEST)
        consumer.start(offset)

        self.assertIsNone(consumer._retry_call)
        fetch_request = FetchRequest(
            topic=topic,
            partition=part,
            offset=offset,
            max_bytes=FETCH_BUFFER_SIZE_BYTES,
        )
        consumer.client.send_fetch_request.assert_called_once_with([fetch_request], max_wait_time=100, min_bytes=65536)

        f = Failure(OffsetOutOfRangeError())
        reqs_d.errback(f)

        self.assertEqual(consumer._fetch_offset, OFFSET_LATEST)
        self.assertIsNotNone(consumer._retry_call)

        latest_offset_request = OffsetRequest(topic, part, OFFSET_LATEST, 1)

        with patch.object(kconsumer, "log"):
            consumer._do_fetch()

        consumer.client.send_offset_request.assert_called_once_with([latest_offset_request])

        consumer.stop()

    def test_consumer_offset_out_of_range_error_without_reset(self):
        topic = "offset_out_of_range_error"
        part = 911
        offset = 10000
        fetch_ds = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = fetch_ds

        consumer = Consumer(mockclient, topic, part, Mock())
        d = consumer.start(offset)

        f = Failure(OffsetOutOfRangeError())
        fetch_ds[0].errback(f)

        self.assertEqual(self.failureResultOf(d), f)

        consumer.stop()

    def test_consumer_errors_during_offset(self):
        attempts = 5
        topic = "all_errors_during_offset"
        part = 991
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_request.side_effect = Deferred
        # The request we expect...
        request = OffsetRequest(topic, part, OFFSET_EARLIEST, 1)
        # The error we'll return
        f = Failure(KafkaUnavailableError())  # Perhaps kafka wasn't up yet...

        consumer = Consumer(mockclient, topic, part, Mock(), request_retry_max_attempts=attempts)
        d = consumer.start(OFFSET_EARLIEST)
        # Make sure request for offset was made with correct request
        mockclient.send_offset_request.assert_called_once_with([request])
        call_count = 0
        while not d.called:
            # Make sure more requests are made each time the timer expires
            call_count += 1
            self.assertEqual(call_count, mockclient.send_offset_request.call_count)
            # Errback the request
            consumer._request_d.errback(f)
            # Advance the clock to trigger the next request
            clock.advance(consumer.retry_delay + 0.01)

        self.assertEqual(attempts, call_count)
        # Make sure the start() deferred was errbacked with the failure
        self.assertIs(f, self.failureResultOf(d))
        # Stop the consumer to cleanup any outstanding operations
        consumer.stop()

    def test_consumer_fetch_reply_during_processing(self):
        fetch_ds = [Deferred(), Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        proc_ds = [Deferred(), Deferred()]
        deferMaker = Mock()
        deferMaker.side_effect = proc_ds

        topic = "repl_during_proc"
        part = 42
        offset = 1967

        mockclient.send_fetch_request.side_effect = fetch_ds
        consumer = Consumer(mockclient, topic, part, deferMaker)
        d = consumer.start(offset)
        messages = [create_message(b"v9", b"k9"), create_message(b"v10", b"k10")]

        # Return a message set starting 1 before the requested offest to
        # exercise the message-skipping code in Consumer._handle_fetch_response
        # which deals with the fact that Kafka can return messages with offests
        # less than requested due to messages being compressed as a set, and
        # the whole compressed set being returned together
        message_set = KafkaCodec._encode_message_set(messages, offset - 1)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 486, message_iter)]
        with patch.object(kconsumer, "log"):
            fetch_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_ds[0], consumer._processor_d)
        # Trigger another fetch
        clock.advance(0.01)
        # Make sure the consumer made a 2nd fetch request
        self.assertEqual(fetch_ds[1], consumer._request_d)
        # Make sure the consumer is still waiting on the 1st processor deferred
        self.assertEqual(proc_ds[0], consumer._processor_d)
        # Deliver the 2nd fetch result
        message_set = KafkaCodec._encode_message_set(messages, offset + 1)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 486, message_iter)]
        fetch_ds[1].callback(responses)
        # Make sure the consumer is STILL waiting on the 1st processor deferred
        self.assertEqual(proc_ds[0], consumer._processor_d)
        # And is STILL waiting on the 2nd fetch reply
        self.assertEqual(fetch_ds[1], consumer._request_d)

        # Deliver the processing result
        proc_ds[0].callback(None)
        # Confirm the consumer is now waiting on the 2nd processor deferred,
        # and isn't waiting on any fetch result
        self.assertEqual(proc_ds[1], consumer._processor_d)
        self.assertEqual(None, consumer._request_d)

        # stop consumer to clean up
        self.assertEqual(offset, consumer.stop())
        self.assertEqual(offset, self.successResultOf(d))
        self.assertEqual(offset, consumer.last_processed_offset)
        self.assertIsNone(consumer.last_committed_offset)

    def test_consumer_fetch_large_message(self):
        topic = "fetch_large_message"
        part = 676
        offset = 0
        mock_proc = Mock()
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        reqs_ds = [Deferred() for x in range(10)]
        mockclient.send_fetch_request.side_effect = reqs_ds

        consumer = Consumer(mockclient, topic, part, mock_proc)
        msg_size = consumer.buffer_size * 9
        messages = [create_message(b"*" * msg_size)]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        d = consumer.start(offset)
        log.debug("Started Consumer: %r start_d: %r", consumer, d)

        # Ok, we deliver only part of the message_set, up to the size requested
        while not mock_proc.called:
            # Get the buffer size from the last call
            request = mockclient.send_fetch_request.call_args[0][0][0]
            log.debug("Got request: %r msg size: %d", request, msg_size)
            # Create a response only as large as the 'max_bytes' request param
            message_iter = KafkaCodec._decode_message_set_iter(message_set[0 : request.max_bytes])
            responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 486, message_iter)]
            log.debug(
                "Calling _request_d: %r callback with: %r",
                consumer._request_d,
                responses,
            )
            consumer._request_d.callback(responses)
            # Advance the clock to trigger the next request
            clock.advance(0.1)

        self.assertEqual(0, consumer.stop())
        self.assertEqual(0, self.successResultOf(d))

    def test_consumer_fetch_too_large_message(self):
        topic = "fetch_too_large_message"
        part = 676
        offset = 0
        mock_proc = Mock()
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        reqs_ds = [Deferred() for x in range(10)]
        mockclient.send_fetch_request.side_effect = reqs_ds

        consumer = Consumer(
            mockclient,
            topic,
            part,
            mock_proc,
            max_buffer_size=8 * FETCH_BUFFER_SIZE_BYTES,
        )
        messages = [create_message(b"X" * (consumer.buffer_size * 9))]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        d = consumer.start(offset)

        # Ok, we deliver only part of the message_set, up to the size requested
        while not d.called:
            # Get the buffer size from the last call
            request = mockclient.send_fetch_request.call_args[0][0][0]
            # Create a response only as large as the 'max_bytes' request param
            message_iter = KafkaCodec._decode_message_set_iter(message_set[0 : request.max_bytes])
            responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 486, message_iter)]
            with patch.object(kconsumer, "log"):
                consumer._request_d.callback(responses)
            # Advance the clock to trigger the next request
            clock.advance(0.01)

        self.failureResultOf(d, ConsumerFetchSizeTooSmall)
        consumer.stop()

    def test_consumer_fetch_response_with_wrong_partition(self):
        topic = "fetch_response_with_wrong_partition"
        part = 68
        offset = 0
        mock_proc = Mock()
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        reqs_ds = [Deferred(), Deferred()]
        mockclient.send_fetch_request.side_effect = reqs_ds

        consumer = Consumer(mockclient, topic, part, mock_proc)
        d = consumer.start(offset)

        # Make sure the consumer started
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        self.assertEqual(consumer._request_d, reqs_ds[0])

        # create & deliver the responses
        messages = [create_message(b"v1", b"k1"), create_message(b"v2", b"k2")]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        bad_messages = [create_message(b"fetch_response_with_wrong_partition")]
        bad_message_set = KafkaCodec._encode_message_set(bad_messages, offset)
        bad_message_iter = KafkaCodec._decode_message_set_iter(bad_message_set)
        responses = [
            FetchResponse(topic, part + 1, KAFKA_SUCCESS, 99, bad_message_iter),
            FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter),
        ]
        with patch.object(kconsumer, "log") as klog:
            reqs_ds[0].callback(responses)
            klog.warning.assert_called_once_with(
                "%r: Got response with partition: %r not our own: %r",
                consumer,
                part + 1,
                part,
            )
        # Make sure the processor was called
        self.assertTrue(mock_proc.called)

        self.assertEqual(1, consumer.stop())
        self.assertEqual(1, self.successResultOf(d))

    def test_consumer_do_fetch_not_reentrant(self):
        # This test is a bit of a hack to get coverage
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        consumer = Consumer(mockclient, "do_fetch_not_reentrant", 8, Mock())
        d = consumer.start(0)
        request = FetchRequest("do_fetch_not_reentrant", 8, 0, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )

        # I think _do_fetch() cannot possibly (normally) be called when there's
        # an outstanding request, so force it
        with patch.object(kconsumer, "log") as klog:
            consumer._do_fetch()
            klog.debug.assert_called_once_with("_do_fetch: Outstanding request: %r", consumer._request_d)

        # And make sure no additional fetch request was made
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # clean up
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_do_fetch_before_retry_call(self):
        # This test is a bit of a hack to get coverage
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.return_value = Deferred()
        consumer = Consumer(mockclient, "do_fetch_before_retry_call", 8, Mock())
        d = consumer.start(0)
        request = FetchRequest("do_fetch_before_retry_call", 8, 0, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # The error we'll return
        f = Failure(KafkaUnavailableError())  # Perhaps kafka wasn't up yet...

        # errback the request so the Consumer will create a _retry_call
        with patch.object(kconsumer, "log"):
            consumer._request_d.errback(f)

        # I think _do_fetch() cannot possibly (normally) be called before the
        # retry_call fires, so force it
        with patch.object(kconsumer, "log"):
            consumer._do_fetch()

        # clean up
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_autocommit_during_commit(self):
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        commit_ds = [Deferred(), Deferred()]
        mockclient.send_offset_commit_request.side_effect = commit_ds
        the_group = "XTC"
        the_topic = "test_consumer_autocommit_during_commit"
        the_part = 119
        the_offset = 2496
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs
        consumer._commit_looper = Mock()  # Mock a looping call to test reset
        d = consumer.commit()
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        consumer._commit_looper.reset.assert_called_once_with()
        # Fake start_d, then force an auto-commit
        consumer._start_d = True
        consumer._auto_commit()
        # Check that still only one commit request has been made
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        mockclient.send_offset_commit_request.reset_mock()
        # bump the last_processed_offset
        consumer._last_processed_offset = the_offset + 1  # Fake processed msgs
        # callback the first commit deferred.
        commit_response = [
            OffsetCommitResponse(the_topic, the_part, KAFKA_SUCCESS),
        ]
        commit_ds[0].callback(commit_response)
        self.assertTrue(d.called)
        # Check that the second commit request has been made
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset + 1, TIMESTAMP_INVALID, None)
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )

    def test_consumer_unhandled_commit_failure(self):
        """test_consumer_unhandled_commit_failure
        Test that if the commit() call's returned deferred encounters non-kafka
        errors that the commit attempt will not be retried
        """
        the_group = "Bangles"
        the_topic = "test_consumer_unhandled_commit_failure"
        the_part = 6
        the_offset = 4513
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        # Make the commit throw an error that won't allow request to be retried
        the_fail = Failure(ValueError("test_consumer_unhandled_commit_failure induced failure"))
        commit_ds = [fail(the_fail), Deferred()]
        mockclient.send_offset_commit_request.side_effect = commit_ds

        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs

        # Patch the consumer's log so we can make sure the failure is logged
        with patch.object(kconsumer, "log") as klog:
            commit_d = consumer.commit()

        # Make sure send_commit_request was called once, and error was logged
        the_request = OffsetCommitRequest(the_topic, the_part, the_offset, TIMESTAMP_INVALID, None)
        mockclient.send_offset_commit_request.assert_called_once_with(
            the_group, [the_request], consumer_id="", group_generation_id=-1
        )
        klog.error.assert_called_once_with("Unhandleable failure during commit attempt: %r\n\t%r", ANY, ANY)
        self.assertEqual(self.failureResultOf(commit_d, ValueError), the_fail)
        # Eat the error
        commit_d.addErrback(lambda _: None)

    def test_consumer_commit_timer_failed(self):
        """test_consumer_commit_timer_failed
        Test that the looping call is restarted when an error occurs
        Somewhat artificial test to confirm that when failures occur in
        consumer._auto_commit (which cause the looping call to terminate) that
        the looping call is restarted.
        """
        the_group = "Alphaville"
        the_topic = "test_consumer_commit_timer_failed"
        the_part = 5
        the_offset = 5431
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        fetch_ds = [Deferred(), Deferred()]
        mockclient.send_fetch_request.side_effect = fetch_ds
        # Make the commit throw an error that won't allow request to be retried
        the_error = ValueError("test_consumer_commit_timer_failed induced failure")

        # Create a consumer and muck with the state a bit...
        consumer = Consumer(mockclient, the_topic, the_part, Mock(), the_group)
        consumer._last_processed_offset = the_offset  # Fake processed msgs

        # Start the consumer (starts auto-commit clock)
        start_d = consumer.start(the_offset)
        # Patch the consumer's log so we can make sure the failure is logged
        with patch.object(kconsumer, "log") as klog:
            # Advance the clock to trigger auto-commit
            with patch.object(consumer, "commit", side_effect=the_error):
                clock.advance(consumer.auto_commit_every_s)
        klog.warning.assert_called_once_with("_commit_timer_failed: uncaught error %r: %s in _auto_commit", ANY, ANY)

        # Check that the looping call was restarted
        self.assertTrue(consumer._commit_looper.running)

        self.assertEqual(the_offset, consumer.stop())
        self.assertEqual(the_offset, self.successResultOf(start_d))

    def test_consumer_send_timer_stopped_error(self):
        # Purely for coverage
        client = Mock(reactor=MemoryReactorClock())
        consumer = Consumer(client, "topic", 5, Mock(), "abba")
        consumer.start(0)
        with patch.object(kconsumer, "log") as klog:
            consumer._commit_timer_stopped("Borg")
        klog.warning.assert_called_once_with(
            "_commit_timer_stopped with wrong timer:%s not:%s",
            "Borg",
            consumer._commit_looper,
        )
        consumer.stop()

    def test_consumer_send_commit_request_not_concurrent(self):
        # Purely for coverage: Force a call of _send_commit_request
        # in order to effect the raise of OperationInProgress
        client = Mock()
        consumer = Consumer(client, "topic", 5, Mock(), "The Call")
        # Mess with the state
        consumer._last_processed_offset = 1
        the_mock = Mock()
        consumer._commit_req = the_mock
        self.assertRaises(OperationInProgress, consumer.commit)

    def test_consumer_shutdown_nothing_processing_no_cgroup(self):
        """
        Test the consumer shutdown happy path when no messages are currently
        being processed by the processor function (while waiting on fetch req),
        and further that there's no consumer group, so no commit needed
        """
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockproc = Mock()
        consumer = Consumer(mockclient, "snpncgTopic", 1, mockproc)
        start_d = consumer.start(1)
        # Ensure a fetch request was made
        request = FetchRequest("snpncgTopic", 1, 1, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Shutdown the consumer
        shutdown_d = consumer.shutdown()
        # Ensure the stop was signaled
        self.assertIsNone(self.successResultOf(start_d))
        # Ensure the shutdown was signaled
        self.assertIsNone(self.successResultOf(shutdown_d))
        # Ensure the processor was never called
        self.assertFalse(mockproc.called)

    def test_consumer_shutdown_nothing_processing(self):
        """
        Test the consumer shutdown happy path when no messages are currently
        being processed by the processor function (while waiting on fetch req).
        """
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockproc = Mock()
        consumer = Consumer(mockclient, "snpTopic", 1, mockproc, "snpGroup")
        start_d = consumer.start(1)
        # Ensure a fetch request was made
        request = FetchRequest("snpTopic", 1, 1, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Shutdown the consumer
        shutdown_d = consumer.shutdown()
        # Ensure the stop was signaled
        self.assertIsNone(self.successResultOf(start_d))
        # Ensure the shutdown was signaled
        self.assertIsNone(self.successResultOf(shutdown_d))
        # Ensure the processor was never called
        self.assertFalse(mockproc.called)

    def test_consumer_shutdown_processing(self):
        """test_consumer_shutdown_processing
        Test the consumer shutdown happy path when messages are currently
        being processed by the processor function.
        """
        reqs_ds = [Deferred(), Deferred()]
        commit_d = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = reqs_ds
        mockclient.send_offset_commit_request.side_effect = commit_d
        proc_d = Deferred()

        topic = "tcsp"
        part = 2
        offset = 5

        consumer = Consumer(mockclient, topic, part, lambda *args, **kwargs: proc_d, "tcsp_group")
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_d, consumer._processor_d)
        # While the consumer is waiting on the processor_d, shut it down
        shutdown_d = consumer.shutdown()
        # Ensure the processor deferred wasn't cancelled & consumer not stopped
        self.assertFalse(proc_d.called)
        self.assertNoResult(start_d)
        # complete the processor and ensure shutdown completed
        proc_d.callback(None)
        # Indicate a successful commit
        commit_d[0].callback(consumer._last_processed_offset)
        # Ensure the stop was signaled
        self.assertEqual(6, self.successResultOf(start_d))
        # Ensure the shutdown was signaled
        self.assertEqual(6, self.successResultOf(shutdown_d))
        self.assertEqual(6, consumer.last_processed_offset)
        self.assertEqual(6, consumer.last_committed_offset)

    def test_consumer_shutdown_commit_in_progress(self):
        """test_consumer_shutdown_commit_in_progress
        Test the consumer shutdown when there is a commit already in process
        In the case that consumer.shutdown() is called and there is already a
        shutdown request in flight to Kafka, ensure that the shutdown is tied
        to the successful commit of the last processed offset.
        """
        reqs_ds = [Deferred(), Deferred()]
        commit_ds = [Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = reqs_ds
        mockclient.send_offset_commit_request.side_effect = commit_ds
        the_processor = Mock()
        proc_deferreds = [Deferred(), Deferred()]
        the_processor.side_effect = proc_deferreds

        topic = "tcscip"
        part = 3
        offset = 6

        consumer = Consumer(
            mockclient,
            topic,
            part,
            the_processor,
            "tcscip_group",
            auto_commit_every_n=2,
        )
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_deferreds[0], consumer._processor_d)
        # Ensure the processor deferred wasn't cancelled & consumer not stopped
        self.assertFalse(proc_deferreds[0].called)
        # complete the processor which will kick off auto-commit
        proc_deferreds[0].callback(None)
        # While the consumer is waiting on the commit reply, shut it down
        shutdown_d = consumer.shutdown()
        # Ensure consumer was not yet stopped/shutdown not complete
        self.assertFalse(start_d.called)
        self.assertFalse(shutdown_d.called)
        # Indicate a successful commit
        commit_ds[0].callback(consumer._last_processed_offset)
        # Ensure the stop was signaled
        self.assertEqual(7, self.successResultOf(start_d))
        # Ensure the shutdown was signaled
        self.assertEqual(7, self.successResultOf(shutdown_d))

    def test_consumer_shutdown_commit_failure(self):
        """test_consumer_shutdown_commit_failure
        Test the consumer shutdown when the commit attempt fails
        """
        reqs_ds = [Deferred(), Deferred()]
        commit_d = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = reqs_ds
        mockclient.send_offset_commit_request.side_effect = commit_d
        proc_d = Deferred()

        topic = "tcscf"
        part = 2
        offset = 5

        consumer = Consumer(mockclient, topic, part, lambda *args, **kwargs: proc_d, "tcscf_group")
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_d, consumer._processor_d)
        # While the consumer is waiting on the processor_d, shut it down
        shutdown_d = consumer.shutdown()
        # Ensure the processor deferred wasn't cancelled & consumer not stopped
        self.assertNoResult(proc_d)
        # complete the processor and ensure shutdown completed
        proc_d.callback(None)
        # Indicate a failed commit
        the_fail = Failure(RuntimeError("Unretryable Commit Failure"))
        commit_d[0].errback(the_fail)
        # Ensure the stop was signaled with nothing committed
        self.assertEqual(6, self.successResultOf(start_d))
        self.assertEqual(6, consumer.last_processed_offset)
        self.assertIsNone(consumer.last_committed_offset)
        # Ensure the shutdown was signaled as an errback
        self.assertEqual(the_fail, self.failureResultOf(shutdown_d))

    def test_consumer_shutdown_processor_failure(self):
        """test_consumer_shutdown_processor_failure
        Test the consumer shutdown when the processor fails/errbacks after
        shutdown is called
        """
        reqs_ds = [Deferred(), Deferred()]
        commit_d = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = reqs_ds
        mockclient.send_offset_commit_request.side_effect = commit_d
        the_processor = Mock()
        proc_deferreds = [Deferred(), Deferred()]
        the_processor.side_effect = proc_deferreds

        topic = "tcspf"
        part = 3
        offset = 8

        consumer = Consumer(mockclient, topic, part, the_processor, "tcspf_group", auto_commit_every_n=1)
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_deferreds[0], consumer._processor_d)
        # While the consumer is waiting on the processor_d, shut it down
        shutdown_d = consumer.shutdown()
        # Ensure the processor deferred wasn't cancelled & consumer not stopped
        self.assertFalse(proc_deferreds[0].called)
        self.assertNoResult(start_d)
        # errback the processor
        the_fail = Failure(RuntimeError("Horrible Processor Failure"))
        proc_deferreds[0].errback(the_fail)
        # Ensure the stop was signaled with the failure
        self.assertEqual(self.failureResultOf(start_d), the_fail)
        # Ensure the shutdown was signaled as a callback, not errback
        self.assertIsNone(self.successResultOf(shutdown_d))
        self.assertIsNone(consumer.last_processed_offset)
        self.assertIsNone(consumer.last_committed_offset)

    def test_consumer_shutdown_processor_immediate_shutdown(self):
        """
        Test the consumer when the processor calls shutdown immediately.
        Any in-progress operations should be completed, and the start and
        shutdown deferreds should return the proper committed/processed offsets
        """
        reqs_ds = [Deferred(), Deferred()]
        commit_d = [Deferred()]
        proc_d_canceller = Mock()
        proc_d_errmock = Mock()
        proc_deferred = Deferred(canceller=proc_d_canceller)
        proc_deferred.addErrback(proc_d_errmock)
        proc_l = []  # Used to pass the shutdown_d out
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = reqs_ds
        mockclient.send_offset_commit_request.side_effect = commit_d

        topic = "tcspis"
        part = 5
        offset = 9

        def the_processor(consumer, messages):
            # Shutdown the consumer, and use proc_l to pass out the deferred
            d = consumer.shutdown()
            proc_l.append(d)
            # Even though we've called shutdown, return a deferred anyway...
            return proc_deferred

        consumer = Consumer(
            mockclient,
            topic,
            part,
            the_processor,
            "tcspis_group",
            auto_commit_every_n=1,
        )
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # The processor shutdown the consumer prior to returning, so
        # stop/shutdown should have run, and the processor deferred
        # should have been cancelled. Check that's true
        proc_d_canceller.assert_called_once_with(proc_deferred)
        # Since the canceller didn't callback/errback, proc_deferred, it should
        # be errback'd by Twisted with a CancelledError
        commit_fail = proc_d_errmock.mock_calls[0][1][0]
        assert isinstance(commit_fail, Failure)
        commit_fail.trap(CancelledError)
        # Ensure the stop (start_d) was signaled with success
        self.assertIsNone(self.successResultOf(start_d))
        # Ensure the shutdown was signaled as a callback, not errback
        self.assertIsNone(self.successResultOf(proc_l[0]))

    def test_consumer_shutdown_called_twice(self):
        """
        Test the consumer shutdown when there is a shutdown already in progress
        """
        reqs_ds = [Deferred(), Deferred()]
        commit_d = [Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = reqs_ds
        mockclient.send_offset_commit_request.side_effect = commit_d
        proc_d = Deferred()

        topic = "csct"
        part = 2
        offset = 5

        consumer = Consumer(mockclient, topic, part, lambda *args, **kwargs: proc_d, "csct_group")
        start_d = consumer.start(offset)
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # create & deliver the response
        messages = [
            create_message(b"v1", b"k1"),
            create_message(b"v2", b"k2"),
        ]
        message_set = KafkaCodec._encode_message_set(messages, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        responses = [FetchResponse(topic, part, KAFKA_SUCCESS, 99, message_iter)]
        reqs_ds[0].callback(responses)
        # Make sure the processor was called
        self.assertEqual(proc_d, consumer._processor_d)
        # While the consumer is waiting on the processor_d, shut it down
        shutdown_d = consumer.shutdown()
        # Ensure the processor deferred wasn't cancelled & consumer not stopped
        self.assertFalse(proc_d.called)
        self.assertNoResult(start_d)
        self.assertFalse(shutdown_d.called)
        # While the consumer is waiting for the processor to complete, call
        # shutdown again and assert it raises a RestopError
        shutdown_d_2 = consumer.shutdown()
        the_fail = self.failureResultOf(shutdown_d_2, RestopError)
        self.assertEqual(the_fail.value.args, ("Shutdown called more than once.",))
        # Complete the shutdown.
        proc_d.callback(None)
        commit_d[0].callback(consumer._last_processed_offset)
        # Ensure the stop (start_d) was signaled with success
        self.assertEqual(6, self.successResultOf(start_d))
        # Ensure the shutdown was signaled as a callback, not errback
        self.assertEqual(6, self.successResultOf(shutdown_d))

    def test_consumer_shutdown_when_not_started(self):
        """
        Test the consumer shutdown when the consumer was never started
        """
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockproc = Mock()
        consumer = Consumer(mockclient, "cswns", 1, mockproc)
        shutdown_d = consumer.shutdown()
        the_fail = self.failureResultOf(shutdown_d, RestopError)
        self.assertEqual(the_fail.value.args, ("Shutdown called on non-running consumer",))

    def test_consumer_commit_with_retrieved_offset(self):
        """
        Test that the consumer properly handles a commit operation from
        the processor function before deferred fires, after having retrieved
        committed offsets from Kafka. This tests that a bug which
        existed previously (<=v2.6.0) is fixed.
        """
        offset = 1234  # arbitrary, offset committed on topic
        fetch_offset = offset + 1  # fetch at next offset after committed
        highwatermark = offset + 100  # last message in topic/part
        topic = "topic_with_committed_offsets"
        part = 56
        offset_fetch_ds = [Deferred()]
        fetch_ds = [Deferred(), Deferred()]
        proc_ds = [Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_fetch_request.side_effect = offset_fetch_ds
        mockclient.send_fetch_request.side_effect = fetch_ds
        the_processor = Mock()
        the_processor.side_effect = proc_ds
        consumer = Consumer(
            mockclient,
            topic,
            part,
            the_processor,
            consumer_group="myGroup",
            auto_commit_every_n=0,
            auto_commit_every_ms=0,
        )
        start_d = consumer.start(OFFSET_COMMITTED)
        # Make sure request was made
        request = OffsetFetchRequest(topic, part)
        mockclient.send_offset_fetch_request.assert_called_once_with("myGroup", [request])
        # Deliver the response
        responses = [OffsetFetchResponse(topic, part, offset, b"METADATA", KAFKA_SUCCESS)]
        offset_fetch_ds[0].callback(responses)
        self.assertEqual(fetch_offset, consumer._fetch_offset)
        # Check that the message fetch was started
        request = FetchRequest(topic, part, fetch_offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Fake the fetch response to trigger the processor call
        # create & deliver the response
        messages = [create_message(b"v1", b"k1")]
        message_set = KafkaCodec._encode_message_set(messages, fetch_offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)
        fetch_responses = [FetchResponse(topic, part, KAFKA_SUCCESS, highwatermark, message_iter)]
        fetch_ds[0].callback(fetch_responses)
        # Check that the processor function was properly called
        log.debug("MockClient Calls: %r ", mockclient.mock_calls)
        the_processor.assert_called_once_with(consumer, [SourcedMessage(topic, part, fetch_offset, messages[0])])
        # Attempt to commit offsets
        commit_d = consumer.commit()
        # the commit call should have short-circuited due to lack of
        # processing anything up to now.
        self.assertEqual(1234, self.successResultOf(commit_d))
        self.assertFalse(mockclient.send_offset_commit_request.called)
        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(start_d))
        self.assertIsNone(consumer.last_processed_offset)
        self.assertEqual(1234, consumer.last_committed_offset)

    def test_consumer_consume_committed_no_offset_stored(self):
        """
        Test that when a consumer is started from OFFSET_COMMITTED and there
        is no committed offset that the fetch request is for OFFSET_EARLIEST,
        not 0 or any other offset

        https://github.com/ciena/afkak/issues/13
        """
        topic = "notCommittedTopic"
        part = 0
        offset = 20170912
        group = "aGroup"
        reqs_ds = [Deferred(), Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_fetch_request.return_value = reqs_ds[0]
        mockclient.send_offset_request.return_value = reqs_ds[1]
        mockclient.send_fetch_request.return_value = reqs_ds[2]
        consumer = Consumer(mockclient, topic, part, Mock(), consumer_group=group)
        d = consumer.start(OFFSET_COMMITTED)
        # Make sure request for committed offset was made
        request = OffsetFetchRequest(topic, part)
        mockclient.send_offset_fetch_request.assert_called_once_with(group, [request])
        # Deliver the response. -1 offset, empty metadata
        responses = [OffsetFetchResponse(topic, part, -1, "", KAFKA_SUCCESS)]
        reqs_ds[0].callback(responses)
        self.assertEqual(OFFSET_EARLIEST, consumer._fetch_offset)
        # Make sure request for OFFSET_EARLIEST was made
        request = OffsetRequest(topic, part, OFFSET_EARLIEST, 1)
        mockclient.send_offset_request.assert_called_once_with([request])
        # Deliver the response. -1 offset, empty metadata
        responses = [OffsetResponse(topic, part, KAFKA_SUCCESS, [offset])]
        reqs_ds[1].callback(responses)
        self.assertEqual(offset, consumer._fetch_offset)
        # Check that the message fetch was started
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_consume_committed_no_offset_auto_offset_reset(self):
        """
        Test that when a consumer is started from OFFSET_COMMITTED and there
        is no committed offset that the fetch request is aligned with the
        passed in value of auto_offset_reset (OFFSET_EARLIEST or OFFSET_LATEST)

        https://github.com/ciena/afkak/issues/14
        """
        topic = "notCommittedTopic"
        part = 0
        offset = 20200904
        group = "aGroup1"
        reqs_ds = [Deferred(), Deferred(), Deferred()]
        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_offset_fetch_request.return_value = reqs_ds[0]
        mockclient.send_offset_request.return_value = reqs_ds[1]
        mockclient.send_fetch_request.return_value = reqs_ds[2]
        consumer = Consumer(
            mockclient,
            topic,
            part,
            Mock(),
            consumer_group=group,
            auto_offset_reset=OFFSET_LATEST,
        )
        d = consumer.start(OFFSET_COMMITTED)
        # Make sure request for committed offset was made
        request = OffsetFetchRequest(topic, part)
        mockclient.send_offset_fetch_request.assert_called_once_with(group, [request])
        # Deliver the response. -1 offset, empty metadata
        responses = [OffsetFetchResponse(topic, part, -1, b"", KAFKA_SUCCESS)]
        reqs_ds[0].callback(responses)
        self.assertEqual(OFFSET_LATEST, consumer._fetch_offset)
        # Make sure request for OFFSET_LATEST was made
        request = OffsetRequest(topic, part, OFFSET_LATEST, 1)
        mockclient.send_offset_request.assert_called_once_with([request])
        # Deliver the response. -1 offset, empty metadata
        responses = [OffsetResponse(topic, part, KAFKA_SUCCESS, [offset])]
        reqs_ds[1].callback(responses)
        self.assertEqual(offset, consumer._fetch_offset)
        # Check that the message fetch was started
        request = FetchRequest(topic, part, offset, consumer.buffer_size)
        mockclient.send_fetch_request.assert_called_once_with(
            [request],
            max_wait_time=consumer.fetch_max_wait_time,
            min_bytes=consumer.fetch_min_bytes,
        )
        # Stop the consumer to cleanup any outstanding operations
        self.assertIsNone(consumer.stop())
        self.assertIsNone(self.successResultOf(d))

    def test_consumer_process_messages_should_exit_when_no_messages_left(self):
        client = Mock()
        a_partition = 9
        a_processor = Mock()
        a_consumer_group = "My Consumer Group"

        consumer = Consumer(
            client,
            "a_topic",
            a_partition,
            a_processor,
            a_consumer_group,
        )
        self.assertIsNone(self.successResultOf(consumer._process_messages([])))

    def test_consumer_process_messages_stack_safe(self):
        """
        The Consumer process a list of messages without causing a RecursionError
        Past versions of afkak used a callback chain to process fetched messages.
        """

        count = sys.getrecursionlimit() + 1

        class MessageProcessor(object):
            calls = 0

            def __call__(self, consumer, messages):
                self.calls += 1
                return succeed(None)

        # set up a Consumer and start it so it can consume messages

        offset = 0
        topic = "some_topic"
        partition = 13
        consumer_group = "A nice day"

        message_list = [create_message(b"paylod", b"key") for _ in range(count)]
        message_set = KafkaCodec._encode_message_set(message_list, offset)
        message_iter = KafkaCodec._decode_message_set_iter(message_set)

        clock = MemoryReactorClock()
        mockclient = Mock(reactor=clock)
        mockclient.send_fetch_request.side_effect = [
            succeed([FetchResponse(topic, partition, KAFKA_SUCCESS, 5, message_iter)]),
        ]

        processor = MessageProcessor()

        # Instantiate a Consumer with auto_commit_every_n=1 to force batches of messages of
        # size 1 to be sent to the processor, in order to be able to make an assertion about the
        # the number of calls made to the message processor

        consumer = Consumer(
            mockclient,
            topic,
            partition,
            processor,
            consumer_group,
            auto_commit_every_n=1,
        )
        consumer.start(offset)

        # A RecursionError during processing will cause the callback chain to stop, which
        # will result in messages without a corresponding call to the message processor

        self.assertEqual(count, processor.calls)

    def test_consumer_process_messages_should_notify_msg_block_when_no_messages_left(
        self,
    ):
        client = Mock()
        a_partition = 9
        a_processor = Mock()
        a_consumer_group = "My Consumer Group"

        consumer = Consumer(
            client,
            "a_topic",
            a_partition,
            a_processor,
            a_consumer_group,
        )
        d = consumer._msg_block_d = Deferred()
        consumer._process_messages([])
        self.assertTrue(self.successResultOf(d))
