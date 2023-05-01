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
import logging
import pprint
from unittest.mock import Mock, patch

from afkak import ConsumerGroup
from afkak._group import Coordinator, _ConsumerProtocol, _NeedTopicPartitions
from afkak.common import (
    CoordinatorNotAvailable,
    IllegalGeneration,
    InconsistentGroupProtocol,
    InvalidGroupId,
    NotCoordinator,
    RebalanceInProgress,
    RequestTimedOutError,
    RestartError,
    RestopError,
    UnknownError,
    _HeartbeatResponse,
    _JoinGroupRequestProtocol,
    _JoinGroupResponse,
    _JoinGroupResponseMember,
    _SyncGroupRequestMember,
    _SyncGroupResponse,
)
from afkak.kafkacodec import KafkaCodec

from twisted.internet import defer, task
from twisted.python.failure import Failure
from twisted.trial import unittest

log = logging.getLogger(__name__)


def assert_delayed_calls(n, client):
    """
    :param int n: Expected number of delayed calls.
    :param client:
        An object like `afkak.client.KafkaClient` which has a *reactor*
        attribute.
    """
    calls = client.reactor.getDelayedCalls()
    calls_repr = pprint.pformat([vars(c) for c in calls])
    log.debug("assert_delayed_calls(%d) found calls %s", n, calls_repr)
    if len(calls) == n:
        return
    raise AssertionError(
        ("Expected {expected} delayed calls at time {now}," " but found {actual}:\n{calls}").format(
            expected=n,
            actual=len(calls),
            now=client.reactor.seconds(),
            calls=calls_repr,
        )
    )


class Base(unittest.TestCase):
    def mock_client(self, coordinator_responses, topic_partitions=None):
        if topic_partitions is None:
            topic_partitions = {"topic1": [0, 1]}

        def _load_topic_partitions(*topics):
            # NB: Will KeyError for unknown topics, which isn't exactly realistic.
            result = {topic: topic_partitions[topic] for topic in topics}
            return defer.succeed(result)

        client = Mock()
        client.reactor = task.Clock()
        client._send_request_to_coordinator.side_effect = coordinator_responses
        client._load_topic_partitions.side_effect = _load_topic_partitions
        return client

    def make_coordinator(self, client):
        return Coordinator(client, "group_id", ["topic1"])

    def join_response(self, member_id="m1", leader_id="m1"):
        return defer.succeed(
            _JoinGroupResponse(
                error=0,
                generation_id="g1",
                group_protocol="consumer",
                member_id=member_id,
                leader_id=leader_id,
                members=[
                    _JoinGroupResponseMember(
                        member_id,
                        member_metadata=b"\x00\x00\x00\x00\x00\x01\x00\x06topic1\x00\x00\x00\x00",
                    ),
                ],
            )
        )

    def sync_response(self, member_id="m1", assignments=None):
        if assignments is None:
            assignments = {"topic1": [0, 1]}  # All partitions per the default topic_partitions
        return defer.succeed(
            _SyncGroupResponse(
                error=0,
                member_assignment=KafkaCodec.encode_sync_group_member_assignment(
                    version=0,
                    assignments=assignments,
                    user_data=b"",
                ),
            )
        )


class TestCoordinator(Base):
    def test_send_join_group_request_success(self):
        client = self.mock_client(
            [
                self.join_response(),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.send_join_group_request()
        self.successResultOf(de)
        self.assertEqual(coord.member_id, "m1")
        self.assertEqual(coord.leader_id, "m1")

    def test_send_join_group_request_failure(self):
        client = self.mock_client(
            [
                defer.fail(RebalanceInProgress()),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.send_join_group_request()
        self.successResultOf(de)
        self.assertEqual(coord.member_id, "")
        self.assertEqual(coord.leader_id, None)
        self.assertEqual(coord._rejoin_needed, True)
        self.assertIn("rejoin_needed", repr(coord))

    def test_send_sync_group_request_success(self):
        client = self.mock_client(
            [
                self.join_response(),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.send_sync_group_request([])
        self.successResultOf(de)

    def test_send_sync_group_request_failure(self):
        client = self.mock_client(
            [
                defer.fail(RebalanceInProgress()),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.send_sync_group_request([])
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, True)

    def test_join_no_broker(self):
        """
        fail to retrieve the coordinator broker and retry
        """
        client = self.mock_client([])
        coord = self.make_coordinator(client)
        coord.get_coordinator_broker = Mock(return_value=None)

        de = coord.join_and_sync()
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, True)

    def test_join_sync_leader(self):
        """
        Successfully join, assign as leader, and sync
        """
        client = self.mock_client(
            [
                self.join_response(),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.join_and_sync()
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, False)
        self.assertEqual(coord.member_id, "m1")
        self.assertEqual(coord.leader_id, "m1")
        self.assertIn("joined", repr(coord))

    def test_join_sync_follower(self):
        """
        Successfully join and sync as follower
        """
        client = self.mock_client(
            [
                self.join_response(leader_id="m2"),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.join_and_sync()
        self.successResultOf(de)

    def test_join_error(self):
        """
        Get an error when joining and retry
        """
        client = self.mock_client(
            [
                defer.fail(RebalanceInProgress()),
                self.join_response(),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.join_and_sync()
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, True)
        assert_delayed_calls(1, client)
        de = coord.join_and_sync()
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, False)

    def test_sync_error(self):
        """
        Get an error when syncing and retry
        """
        client = self.mock_client(
            [
                self.join_response(),
                defer.fail(RebalanceInProgress()),
                self.join_response(),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)
        de = coord.join_and_sync()
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, True)
        assert_delayed_calls(1, client)
        de = coord.join_and_sync()
        self.successResultOf(de)
        self.assertEqual(coord._rejoin_needed, False)

    def test_join_fatal_error(self):
        """
        Get an unexpected (non-kafka) error when joining and propagate it
        """
        client = self.mock_client(
            [
                defer.fail(AttributeError()),
            ]
        )
        coord = self.make_coordinator(client)
        start_d = coord._start_d = defer.Deferred()
        de = coord.join_and_sync()
        self.failureResultOf(start_d)
        self.successResultOf(de)

    def test_join_not_needed(self):
        """
        call join_and_sync when not needed
        """
        client = self.mock_client([])
        coord = self.make_coordinator(client)
        _join = coord._join_and_sync = Mock()
        coord._rejoin_d = defer.Deferred()
        coord.join_and_sync()
        _join.assert_not_called()
        coord._rejoin_d = None
        coord._rejoin_needed = False
        coord.join_and_sync()
        _join.assert_not_called()

    def test_join_fatal_exception(self):
        """
        have an exception come out of _join_and_sync
        """
        client = self.mock_client([])
        coord = self.make_coordinator(client)
        coord._start_d = defer.Deferred()
        coord._join_and_sync = Mock(return_value=defer.Deferred())
        result = coord.join_and_sync()
        result.errback(ArithmeticError())
        self.successResultOf(result)

    def test_heartbeat_resync(self):
        """
        run successful heartbeats, get a resync message, and schedule resync
        """
        client = self.mock_client(
            [
                self.join_response(),
                self.sync_response(),
                defer.succeed(_HeartbeatResponse(error=0)),
                defer.fail(RebalanceInProgress()),
                self.join_response(),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)
        self.successResultOf(coord.join_and_sync())
        self.assertEqual(coord._rejoin_needed, False)
        assert_delayed_calls(1, client)

        # Run the heartbeat, which succeeds.
        client.reactor.advance(5.0)
        self.assertEqual(coord._rejoin_needed, False)
        self.assertFalse(coord._heartbeat_request_d)

        # Run a heartbeat, which fails with RebalanceInProgress
        client.reactor.advance(5.0)
        self.assertEqual(coord._rejoin_needed, True)
        self.assertFalse(coord._heartbeat_request_d)
        # There is a delayed call to join_and_sync():
        assert_delayed_calls(1, client)
        self.assertEqual(coord._rejoin_needed, True)

        client.reactor.advance(0.1)  # Call join_and_sync()
        self.assertEqual(coord._rejoin_needed, False)
        assert_delayed_calls(1, client)  # Heartbeat scheduled.

        self.assertIs(None, coord.join_and_sync())
        self.assertEqual(coord._rejoin_needed, False)

    def test_heartbeat_coordinator_lost(self):
        """
        run a heartbeat that indicates the coordinator has changed
        """
        client = self.mock_client(
            [
                self.join_response(),
                self.sync_response(),
                defer.fail(NotCoordinator()),
            ]
        )
        coord = self.make_coordinator(client)
        self.successResultOf(coord.join_and_sync())

        self.assertEqual(coord._rejoin_needed, False)

        assert_delayed_calls(1, client)
        client.reactor.advance(6.0)
        self.assertFalse(coord._heartbeat_request_d)
        assert_delayed_calls(1, client)
        self.assertEqual(coord._rejoin_needed, True)
        client.reset_consumer_group_metadata.assert_called_with(coord.group_id)

    def test_heartbeat_in_progress(self):
        """
        get a heartbeat timer tick while a request is already in progress
        """
        client = self.mock_client(
            [
                self.join_response(),
                self.sync_response(),
                defer.succeed(_HeartbeatResponse(error=0)),
            ]
        )
        coord = self.make_coordinator(client)
        self.successResultOf(coord.join_and_sync())
        self.assertEqual(coord._rejoin_needed, False)

        assert_delayed_calls(1, client)  # Heartbeat scheduled.

        coord._heartbeat_request_d = defer.Deferred()
        coord.send_heartbeat_request = Mock()
        client.reactor.advance(5.0)
        coord.send_heartbeat_request.assert_not_called()
        coord._heartbeat_request_d = None
        coord._stopping = True
        coord._heartbeat()
        coord.send_heartbeat_request.assert_not_called()
        coord._stopping = False
        coord._heartbeat()
        coord.send_heartbeat_request.assert_any_call()

    def test_leave(self):
        """
        send a leavegroup message
        """
        client = self.mock_client(
            [
                defer.succeed(Mock(error_code=0)),
            ]
        )
        coord = self.make_coordinator(client)
        coord.coordinator_broker = Mock()
        coord.member_id = "m1"
        coord.generation_id = "g1"
        de = coord.send_leave_group_request()
        self.successResultOf(de)
        self.assertEqual(coord.member_id, "")
        self.assertIsNone(coord.generation_id)

    def test_start_stop(self):
        client = self.mock_client(
            [
                self.join_response(),
                self.sync_response(),
            ]
        )
        coord = self.make_coordinator(client)

        start_d = coord.start()
        self.assertNoResult(start_d)
        self.assertRaises(RestartError, coord.start)

        heatbeat_req_d = coord._heartbeat_request_d = defer.Deferred()
        rejoin_wait_d = coord._rejoin_wait_dc = defer.Deferred()
        rejoin_d = coord._rejoin_d = defer.Deferred()
        stop_d = coord.stop()
        self.successResultOf(stop_d)
        self.successResultOf(start_d)
        self.assertEqual(coord.member_id, "")
        self.assertIsNone(coord.protocol)

        self.failureResultOf(heatbeat_req_d)
        self.failureResultOf(rejoin_wait_d)
        self.failureResultOf(rejoin_d)

    def test_stop_not_started(self):
        client = self.mock_client([])
        coord = self.make_coordinator(client)
        stop = coord.stop()
        self.failureResultOf(stop, RestopError)

    def test_double_stop(self):
        client = self.mock_client([])
        coord = self.make_coordinator(client)
        coord._start_d = defer.Deferred()
        coord._stopping = True
        double_stop = coord.stop()
        self.failureResultOf(double_stop, RestopError)

    def test_get_coordinator_success(self):
        """
        retrieve a coordinator
        """
        client = self.mock_client(
            [
                defer.succeed(Mock(error_code=0)),
            ]
        )
        coord = self.make_coordinator(client)
        client._get_coordinator_for_group.return_value = defer.succeed(None)
        client._get_brokerclient.return_value = None
        de = coord.get_coordinator_broker()
        self.successResultOf(de)
        self.assertEqual(1, len(client.reactor.getDelayedCalls()))

        client._get_coordinator_for_group.return_value = defer.succeed(Mock())
        client._get_brokerclient.return_value = coord
        client.load_metadata_for_topics.return_value = defer.succeed(None)
        de = coord.get_coordinator_broker()
        result = self.successResultOf(de)
        self.assertEqual(result, coord)

    def test_get_coordinator_retry(self):
        """
        fail to retrieve a coordinator and retry
        """
        client = self.mock_client(
            [
                defer.succeed(Mock(error_code=0)),
            ]
        )
        coord = self.make_coordinator(client)
        client._get_coordinator_for_group.return_value = defer.fail(RequestTimedOutError())
        client._get_brokerclient.return_value = None
        de = coord.get_coordinator_broker()
        self.successResultOf(de)
        assert_delayed_calls(1, client)  # Heartbeat scheduled.
        client._get_coordinator_for_group.return_value = defer.fail(UnknownError())
        de = coord.get_coordinator_broker()
        self.successResultOf(de)

    def test_get_coordinator_fatal(self):
        """
        A non-retriable failure when retrieving the coordinator broker leaves
        the coordinator in a quiescent state.
        """
        client = self.mock_client([])
        client._get_coordinator_for_group.return_value = defer.fail(AttributeError())
        coord = self.make_coordinator(client)

        self.failureResultOf(coord.get_coordinator_broker()).check(AttributeError)

        # No heartbeat scheduled.
        self.assertEqual([], client.reactor.getDelayedCalls())

    def test_rejoin_after_error(self):
        """
        try out all the rejoin_after_error scenarios
        """
        client = self.mock_client([])
        coord = self.make_coordinator(client)
        coord.on_group_leave = Mock()

        def check(rejoin_needed, exc):
            coord._rejoin_needed = False
            coord._rejoin_wait_dc = None
            for call in client.reactor.getDelayedCalls():
                call.cancel()
            client.reset_consumer_group_metadata.reset_mock()
            coord.on_group_leave.reset_mock()

            coord.rejoin_after_error(Failure(exc))
            if rejoin_needed:
                self.assertEqual(coord._rejoin_needed, True)
                assert_delayed_calls(1, client)
            else:
                self.assertEqual(coord._rejoin_needed, False)
                assert_delayed_calls(0, client)
                self.assertEqual(coord._rejoin_wait_dc, None)

        check(True, RebalanceInProgress())
        check(True, CoordinatorNotAvailable())
        client.reset_consumer_group_metadata.assert_any_call(coord.group_id)
        check(True, IllegalGeneration())
        coord.on_group_leave.assert_any_call()
        check(True, InvalidGroupId())
        coord.on_group_leave.assert_any_call()
        check(True, InconsistentGroupProtocol())
        check(True, RequestTimedOutError())
        coord.on_group_leave.assert_any_call()
        check(True, UnknownError())

        coord._stopping = True
        check(False, defer.CancelledError())
        coord._stopping = False

        start_d = coord.start()
        start_d.addErrback(lambda f: None)
        check(False, ValueError())
        coord.on_group_leave.assert_any_call()
        self.successResultOf(start_d)


class ConsumerProtocolTests(unittest.SynchronousTestCase):
    """
    Test `afkak._group._ConsumerProtocol`
    """

    def run_protocol(self, topic_partitions, member_subscriptions):
        """
        Run a single round of the group assignment protocol.

        :param topic_partitions:
            The topic partition metadata as loaded from the broker, a map of
            topic name to partition IDs.
        :type topic_partitions: Mapping[str, List[int]]

        :param members: Map of member ID to subscribed topics
        :type members: Mapping[str, List[str]]

        :returns:
            Map of member ID to topic/partition assignments.
        :rtype: Mapping[str, Mapping[str, List[int]]]
        """
        proto = _ConsumerProtocol()
        # Phase 1: Join group. All members join the group.
        joins = {member_id: proto.join_group_protocols(topics)[0] for member_id, topics in member_subscriptions.items()}
        # Phase 2: Sync group. Leader generates assignments.
        sync_assignments = proto.generate_assignments(
            members=[
                _SyncGroupRequestMember(member_id, joins[member_id].protocol_metadata)
                for member_id in member_subscriptions
            ],
            topic_partitions=topic_partitions,
        )
        # Phase 3: Receive assignments.
        return {
            sync_assign.member_id: proto.decode_assignment(sync_assign.member_metadata)
            for sync_assign in sync_assignments
        }

    def test_join_group_protocols(self):
        proto = _ConsumerProtocol()
        self.assertEqual(
            proto.join_group_protocols(["topic1"]),
            [
                _JoinGroupRequestProtocol(
                    "consumer",
                    b"\x00\x00\x00\x00\x00\x01\x00\x06topic1\x00\x00\x00\x00",
                )
            ],
        )

    def test_single_member(self):
        """
        When there is a single group member it is assigned all of the
        partitions.
        """
        proto = _ConsumerProtocol()

        [join_req] = proto.join_group_protocols(["topic1"])
        [sync_assign] = proto.generate_assignments(
            members=[_SyncGroupRequestMember("member_id", join_req.protocol_metadata)],
            topic_partitions={"topic1": [0, 1, 2, 3]},
        )
        assignment = proto.decode_assignment(sync_assign.member_metadata)

        self.assertEqual({"topic1": (0, 1, 2, 3)}, assignment)

    def test_three_members(self):
        """
        Partitions are distributed evenly among members in a round-robin
        fashion.
        """
        assignments = self.run_protocol(
            topic_partitions={"topic2": [0, 1, 2, 3, 4, 5]},
            member_subscriptions={
                "member1": ["topic2"],
                "member2": ["topic2"],
                "member3": ["topic2"],
            },
        )
        self.assertEqual(
            {
                "member1": {"topic2": (0, 3)},
                "member2": {"topic2": (1, 4)},
                "member3": {"topic2": (2, 5)},
            },
            assignments,
        )

    def test_roundrobin_normal(self):
        assignments = self.run_protocol(
            topic_partitions={"topic1": [0, 1, 2, 3, 4]},
            member_subscriptions={
                "m1": ["topic1"],
                "m2": ["topic1"],
                "m3": ["topic1"],
            },
        )
        self.assertEqual(
            {
                "m1": {"topic1": (0, 3)},
                "m2": {"topic1": (1, 4)},
                "m3": {"topic1": (2,)},
            },
            assignments,
        )

    def test_roundrobin_no_topic(self):
        """
        The protocol refuses to generate empty group assignments. Instead it
        raises a `_NeedTopicPartitions` exception that specifies the required
        topic metatdata.
        """
        with self.assertRaises(_NeedTopicPartitions) as cm:
            self.run_protocol(
                member_subscriptions={
                    "m1": ["topic1"],
                    "m2": ["topic1", "topic2"],
                },
                topic_partitions={},
            )

        self.assertEqual({"topic1", "topic2"}, cm.exception.topics)

    def test_roundrobin_leftover(self):
        """
        If there aren't enough partitions to go around some members may get an
        empty assignment.
        """
        assignments = self.run_protocol(
            topic_partitions={"topic1": [0]},
            member_subscriptions={
                "m1": ["topic1"],
                "m2": ["topic1"],
            },
        )
        self.assertEqual(
            assignments,
            {
                "m1": {"topic1": (0,)},
                "m2": {},
            },
        )

    def test_roundrobin_two_topic(self):
        """
        Group members only receive partitions from topics that they have
        subscribed to.
        """
        assignments = self.run_protocol(
            topic_partitions={"topic1": [0], "topic2": [0, 1]},
            member_subscriptions={
                "m1": ["topic1"],
                "m2": ["topic2"],
            },
        )
        self.assertEqual(
            assignments,
            {
                "m1": {"topic1": (0,)},
                "m2": {"topic2": (0, 1)},
            },
        )


class TestConsumerGroup(Base):
    def test_repr(self):
        """
        ConsumerGroup's repr displays the group name, instance ID (so that
        instances can be differentiated), state, and any member ID assigned by
        the broker.
        """
        group = ConsumerGroup(self.mock_client([]), "group_id", ["topic1"], lambda c, m: None)
        self.assertRegex(
            repr(group),
            r"\A<afkak\.ConsumerGroup 0x[a-f0-9]+ for 'group_id' \[initialized\] member_id=''>\Z",
        )

    def test_start_stop(self):
        """
        start a consumergroup, join, and start consumers
        """
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        group.start()
        group.on_join_prepare()
        group.on_join_complete({"topic1": [1, 2, 3]})
        self.assertEqual(len(group.consumers["topic1"]), 3)
        group.stop()
        self.assertEqual(len(group.consumers), 0)

    def test_start_leave(self):
        """
        start a consumergroup, join, start consumers, then get kicked out
        """
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        group.start()
        group.on_join_prepare()
        group.on_join_complete({"topic1": [1, 2, 3]})
        self.assertEqual(len(group.consumers["topic1"]), 3)
        group.on_group_leave()
        self.assertEqual(len(group.consumers), 0)

    def test_shutdown_error(self):
        """
        get errors while shutting down consumers
        """
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        group.start()
        with patch("afkak._group.Consumer", side_effect=[Mock(), Mock()]):
            group.on_join_complete({"topic1": [1, 2]})
            consumer = group.consumers["topic1"][0]
            consumer._start_d = defer.Deferred()
            consumer.shutdown.side_effect = KeyError()
            consumer.stop.side_effect = KeyError()
            consumer2 = group.consumers["topic1"][1]
            consumer2.shutdown.return_value = defer.Deferred()

        de = group.shutdown_consumers()
        self.assertNoResult(de)
        self.assertEqual(len(group.consumers), 0)

        consumer2.shutdown.return_value.errback(KeyError())
        consumer2.stop.assert_called_once_with()
        self.successResultOf(de)

    def test_stop_error(self):
        """
        get errors while stopping consumers
        """
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        group.start()
        with patch("afkak._group.Consumer"):
            group.on_join_complete({"topic1": [1]})
            consumer = group.consumers["topic1"][0]
            consumer.stop.side_effect = KeyError()
        group.stop_consumers()

    def test_consumer_error(self):
        """
        get an unexpected stop error from a consumer
        """
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        start_d = group.start()
        self.assertNoResult(start_d)
        with patch("afkak._group.Consumer") as mock_consumer:
            mock_consumer.return_value.start.return_value = d = defer.Deferred()
            group.on_join_complete({"topic1": [1]})
            self.assertEqual(mock_consumer.return_value.start.called, True)
            d.errback(Failure(AssertionError()))
            self.failureResultOf(start_d, AssertionError)
            d.addErrback(lambda result: None)

    def test_consumer_cancel_during_shutdown(self):
        """
        get an unexpected CancelledError on the start() deferred
        while shutting down a consumer becasue our heartbeat timed out

        """
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        start_d = group.start()
        with patch("afkak._group.Consumer") as mock_consumer:
            consumer_instance = mock_consumer.return_value
            consumer_start_d = defer.Deferred()
            consumer_instance.start.return_value = consumer_start_d
            consumer_instance._start_d = consumer_start_d
            group.on_join_complete({"topic1": [1]})
            self.assertEqual(consumer_instance.start.called, True)

            def stop():
                consumer_start_d.errback(defer.CancelledError())

            consumer_instance.stop.side_effect = stop
            group.rejoin_after_error(Failure(RequestTimedOutError()))

            self.assertEqual(consumer_instance.stop.called, True)
            self.successResultOf(consumer_start_d)
            self.assertNoResult(start_d)

    def test_rejoin_consumer(self):
        client = self.mock_client([])
        processor = Mock()
        group = ConsumerGroup(client, "group_id", "topic1", processor)
        start_d = group.start()
        group.on_group_leave = Mock()
        with patch("afkak._group.Consumer") as mock_consumer:
            mock_consumer.return_value.start.return_value = d = defer.Deferred()
            group.on_join_complete({"topic1": [1]})
            self.assertEqual(mock_consumer.return_value.start.called, True)
            d.errback(Failure(IllegalGeneration()))
            self.assertEqual(group._rejoin_needed, True)
            self.assertNoResult(start_d)
            group.on_group_leave.assert_any_call()
