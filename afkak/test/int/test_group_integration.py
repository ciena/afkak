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
from collections import defaultdict
from datetime import datetime, timedelta
from unittest.mock import Mock

from afkak import ConsumerGroup, KafkaClient, create_message
from afkak._group import Coordinator
from afkak.common import ProduceRequest
from afkak.test.int.intutil import IntegrationMixin, kafka_versions
from afkak.test.testutil import async_delay

import attr
from twisted.internet.defer import Deferred, DeferredQueue, inlineCallbacks, returnValue
from twisted.trial import unittest

log = logging.getLogger(__name__)


@attr.s(frozen=True)
class Deadline(object):
    timeout = attr.ib(default=timedelta(seconds=30))
    start = attr.ib(default=attr.Factory(datetime.utcnow))

    def check(self):
        """
        Has the deadline expired?

        :raises AssertionError: when the deadline has expired
        """
        elapsed = datetime.utcnow() - self.start
        if elapsed > self.timeout:
            raise AssertionError("Deadline exceeded: {} > {}".format(self.elapsed, self.timeout))


def assert_assignments(topic, partitions, members):
    """
    Are the partitions of a topic properly distributed among the members of the
    group? Specifically:

     -  The expected number of partitions has been assigned.

     -  Partitions are evenly distributed among group members. Some members may
        have an extra partition if the number of partitions doesn't evenly
        divide by the number of members.

     -  Each member's partition assignments are disjoint (each partition is
        assigned to exactly one member).

    :param str topic: Name of the topic to inspect.

    :param int partitions: Number of partitions in the topic.

    :param members: One or more `afkak.ConsumerGroup` instances. Each
        must be configured to consume *topic*.

    :raises AssertionError: when assignments fail validation

    :raises ValueError: when a consumer group isn't consuming the topic at all
    """
    min_count, mod = divmod(partitions, len(members))
    if mod > 0:
        max_count = min_count + 1
    else:
        max_count = min_count

    summary = [
        "topic {!r} has {} partitions".format(topic, partitions),
        "members should own at least {} partitions, no more than {}".format(min_count, max_count),
    ]
    failures = []
    partition_to_members = defaultdict(list)

    for member in members:
        if topic not in member.topics:
            raise ValueError("ConsumerGroup {!r} isn't consuming topic {!r}".format(member, topic))

        a = set(c.partition for c in member.consumers.get(topic, []))
        summary.append("member {} owns partitions {}".format(member, " ".join(str(p) for p in sorted(a))))

        if len(a) < min_count:
            failures.append("too few partitions: {}".format(member))
        elif len(a) > max_count:
            failures.append("too many partitions: {}".format(member))

        for partition in a:
            partition_to_members[partition].append(repr(member))

    for partition, assignees in partition_to_members.items():
        if len(assignees) > 1:
            failures.append("partition {} assignment collision: {}".format(partition, " ".join(assignees)))

    if len(partition_to_members) != partitions:
        failures.append("{} partitions assigned, but expected {}".format(len(partition_to_members), partitions))

    if failures:
        summary.append("\nFailed validation:")
        summary.extend(failures)
    message = "\n".join(summary)
    log.debug(message)
    if failures:
        raise AssertionError(message)


def wait_for_assignments(topic, partitions, members):
    deadline = Deadline()
    while True:
        try:
            assert_assignments(
                topic,
                partitions,
                members,
            )
            break
        except AssertionError:
            deadline.check()
            yield async_delay(0.5)


class TestAfkakGroupIntegration(IntegrationMixin, unittest.TestCase):
    num_partitions = 6
    harness_kw = dict(
        replicas=1,
        partitions=num_partitions,
    )

    @inlineCallbacks
    def send_messages(self, partition, messages):
        log.debug("send_messages(%d, %r)", partition, messages)
        messages = [create_message(self.msg(str(msg))) for msg in messages]
        produce = ProduceRequest(self.topic, partition, messages=messages)
        [resp] = yield self.client.send_produce_request([produce])

        self.assertEqual(resp.error, 0)

        returnValue([x.value for x in messages])

    def when_called(self, object, method):
        """
        returns a Deferred that will be called back after the
        next time the method gets called
        """
        de = Deferred()
        original = getattr(object, method)

        def side_effect(*args, **kwargs):
            ret = original(*args, **kwargs)
            if not de.called:
                de.callback(None)
            return ret

        setattr(object, method, Mock(side_effect=side_effect))
        return de

    @kafka_versions("all")
    @inlineCallbacks
    def test_single_coordinator_join(self):
        coord = Coordinator(self.client, self.id(), ["test-topic"])

        de = self.when_called(coord, "on_join_complete")
        coord.start()
        yield de
        yield coord.stop()

    @kafka_versions("all")
    @inlineCallbacks
    def test_three_coordinator_join(self):
        self.client2 = KafkaClient(self.harness.bootstrap_hosts, clientId=self.topic + "2")
        self.addCleanup(self.client2.close)

        self.client3 = KafkaClient(self.harness.bootstrap_hosts, clientId=self.topic + "3")
        self.addCleanup(self.client3.close)
        coords = [
            Coordinator(
                client,
                self.id(),
                topics=["test-topic"],
                retry_backoff_ms=100,
                heartbeat_interval_ms=100,
                fatal_backoff_ms=1000,
            )
            for client in [self.client, self.client2, self.client3]
        ]
        coords[0].on_join_complete = lambda *args: a_joined.callback(*args)
        coords[0].start()

        a_joined = Deferred()

        # startup the first member of the group
        a_assignment = yield a_joined
        log.warn("first is in")

        self.assertNotEqual(coords[0].generation_id, None)
        self.assertEqual(coords[0].leader_id, coords[0].member_id)
        self.assertEqual(a_assignment, {"test-topic": (0, 1, 2, 3, 4, 5)})
        first_generation_id = coords[0].generation_id

        # now bring someone else into the group
        a_joined, b_joined = Deferred(), Deferred()

        coords[0].on_join_complete = lambda *args: a_joined.callback(*args)
        coords[1].on_join_complete = lambda *args: b_joined.callback(*args)
        log.warn("bringing in second")
        coords[1].start()
        log.warn("waiting for a_joined")
        yield a_joined
        log.warn("waiting for b_joined")
        yield b_joined

        self.assertEqual(coords[0].generation_id, coords[1].generation_id)
        self.assertEqual(coords[0].leader_id, coords[1].leader_id)
        self.assertNotEqual(coords[0].member_id, coords[1].member_id)
        self.assertNotEqual(coords[0].generation_id, first_generation_id)

        # and then bring in a third
        a_joined, b_joined, c_joined = Deferred(), Deferred(), Deferred()

        coords[0].on_join_complete = lambda *args: a_joined.callback(*args)
        coords[1].on_join_complete = lambda *args: b_joined.callback(*args)
        coords[2].on_join_complete = lambda *args: c_joined.callback(*args)
        log.warn("bringing in third")
        coords[2].start()
        log.warn("waiting for a_joined")
        a_assignment = yield a_joined
        log.warn("waiting for b_joined")
        b_assignment = yield b_joined
        log.warn("waiting for c_joined")
        c_assignment = yield c_joined

        self.assertTrue(len(a_assignment["test-topic"]) == 2, a_assignment)
        self.assertTrue(len(b_assignment["test-topic"]) == 2, b_assignment)
        self.assertTrue(len(c_assignment["test-topic"]) == 2, c_assignment)
        self.assertEqual(
            set(a_assignment["test-topic"] + b_assignment["test-topic"] + c_assignment["test-topic"]),
            set(range(6)),
        )
        # and remove one
        a_joined, b_joined = Deferred(), Deferred()

        coords[0].on_join_complete = lambda *args: a_joined.callback(*args)
        coords[1].on_join_complete = lambda *args: b_joined.callback(*args)
        log.warn("removing third")
        yield coords[2].stop()

        log.warn("waiting for a_joined")
        yield a_joined
        log.warn("waiting for b_joined")
        yield b_joined

        log.warn("done")
        yield coords[0].stop()
        yield coords[1].stop()

    @kafka_versions("all")
    @inlineCallbacks
    def test_single_consumergroup_join(self):
        record_stream = DeferredQueue(backlog=1)

        def processor(consumer, records):
            log.debug("processor(%r, %r)", consumer, records)
            record_stream.put(records)

        coord = ConsumerGroup(
            self.client,
            self.id(),
            topics=[self.topic],
            processor=processor,
            retry_backoff_ms=100,
            heartbeat_interval_ms=1000,
            fatal_backoff_ms=3000,
        )
        join_de = self.when_called(coord, "on_join_complete")
        coord.start()
        self.addCleanup(coord.stop)
        yield join_de

        self.assertIn(self.topic, coord.consumers)
        self.assertEqual(len(coord.consumers[self.topic]), self.num_partitions)
        self.assertEqual(coord.consumers[self.topic][0].topic, self.topic)
        self.assertEqual(coord.consumers[self.topic][0].partition, 0)

        for part in range(self.num_partitions):
            values = yield self.send_messages(part, [part])
            msgs = yield record_stream.get()
            self.assertEqual(msgs[0].partition, part)
            self.assertEqual(msgs[0].message.value, values[0])

    @kafka_versions("all")
    @inlineCallbacks
    def test_two_consumergroup_join(self):
        """
        When a second member joins the consumer group it triggers a rebalance.
        After that completes some partitions are distributed to each member.
        """
        group_id = "group_for_two"
        self.client2 = KafkaClient(self.harness.bootstrap_hosts, clientId=self.topic + "2")
        self.addCleanup(self.client2.close)

        record_stream = DeferredQueue(backlog=1)

        def processor(consumer, records):
            log.debug("processor(%r, %r)", consumer, records)
            record_stream.put(records)

        coord = ConsumerGroup(
            self.client,
            group_id,
            topics=[self.topic],
            processor=processor,
            retry_backoff_ms=100,
            heartbeat_interval_ms=1000,
            fatal_backoff_ms=3000,
        )
        de = self.when_called(coord, "on_join_complete")
        coord_start_d = coord.start()
        self.addCleanup(coord.stop)

        # FIXME: This doesn't seem to get fired reliably.
        coord_start_d # noqa
        # self.addCleanup(lambda: coord_start_d)

        yield de

        # send some messages and see that they're processed
        for part in range(self.num_partitions):
            values = yield self.send_messages(part, [part])
            msgs = yield record_stream.get()
            self.assertEqual(msgs[0].partition, part)
            self.assertEqual(msgs[0].message.value, values[0])

        coord2 = ConsumerGroup(
            self.client2,
            group_id,
            topics=[self.topic],
            processor=processor,
            retry_backoff_ms=100,
            heartbeat_interval_ms=1000,
            fatal_backoff_ms=3000,
        )
        de = self.when_called(coord, "on_join_complete")
        de2 = self.when_called(coord2, "on_join_complete")
        coord2_start_d = coord2.start()
        self.addCleanup(coord2.stop)

        # FIXME: This doesn't seem to get fired reliably
        coord2_start_d # noqa
        # self.addCleanup(lambda: coord2_start_d)

        yield de
        yield de2
        self.assertIn(self.topic, coord.consumers)
        self.assertIn(self.topic, coord2.consumers)
        self.assertEqual(len(coord.consumers[self.topic]), 3)
        self.assertEqual(len(coord2.consumers[self.topic]), 3)
        self.assertNotEqual(
            coord.consumers[self.topic][0].partition,
            coord2.consumers[self.topic][0].partition,
        )

        # after the cluster has re-formed, send some more messages
        # and check that we get them too (and don't get the old messages again)
        for part in range(self.num_partitions):
            values = yield self.send_messages(part, [part])
            msgs = yield record_stream.get()
            self.assertEqual(msgs[0].partition, part)
            self.assertEqual(msgs[0].message.value, values[0])

    @kafka_versions("all")
    @inlineCallbacks
    def test_broker_restart(self):
        """
        restart the kafka broker and verify that the group rejoins
        """
        record_stream = DeferredQueue(backlog=1)

        def processor(consumer, records):
            log.debug("processor(%r, %r)", consumer, records)
            record_stream.put(records)

        coord = ConsumerGroup(
            self.client,
            self.id(),
            topics=[self.topic],
            processor=processor,
            retry_backoff_ms=100,
            heartbeat_interval_ms=1000,
            fatal_backoff_ms=2000,
        )
        join_de = self.when_called(coord, "on_join_complete")
        coord.start()
        self.addCleanup(coord.stop)

        yield join_de
        self.assertIn(self.topic, coord.consumers)
        self.assertEqual(len(coord.consumers[self.topic]), self.num_partitions)
        self.assertEqual(coord.consumers[self.topic][0].topic, self.topic)
        self.assertEqual(coord.consumers[self.topic][0].partition, 0)

        # restart the broker and see that we re-join and still work
        leave_de = self.when_called(coord, "on_group_leave")
        prepare_de = self.when_called(coord, "on_join_prepare")
        join_de = self.when_called(coord, "on_join_complete")
        self.harness.brokers[0].stop()
        yield leave_de
        self.assertEqual(len(coord.consumers), 0)
        self.harness.brokers[0].restart()
        yield prepare_de
        yield join_de
        self.assertIn(self.topic, coord.consumers)
        self.assertEqual(len(coord.consumers[self.topic]), self.num_partitions)

        for part in range(self.num_partitions):
            values = yield self.send_messages(part, [part])
            msgs = yield record_stream.get()
            self.assertEqual(msgs[0].partition, part)
            self.assertEqual(msgs[0].message.value, values[0])

    @kafka_versions("all")
    @inlineCallbacks
    def test_consumer_rejoin(self):
        """
        trigger a rejoin via consumer commit failure
        """
        group = "rejoin_group"
        self.client2 = KafkaClient(self.harness.bootstrap_hosts, clientId=self.topic + "2")
        self.addCleanup(self.client2.close)

        record_stream = DeferredQueue(backlog=1)

        def processor(consumer, records):
            log.debug("processor(%r, %r)", consumer, records)
            record_stream.put(records)

        coord = ConsumerGroup(
            self.client,
            group,
            topics=[self.topic],
            processor=processor,
            session_timeout_ms=6000,
            retry_backoff_ms=100,
            heartbeat_interval_ms=1000,
            fatal_backoff_ms=3000,
            consumer_kwargs=dict(auto_commit_every_ms=1000),
        )
        coord_start_d = coord.start()
        self.addCleanup(coord.stop)
        # FIXME: This doesn't seem to get fired reliably.
        coord_start_d # noqa
        # self.addCleanup(lambda: coord_start_d)

        yield wait_for_assignments(self.topic, self.num_partitions, [coord])

        # kill the heartbeat timer and start joining the second consumer
        while True:
            if coord._heartbeat_looper.running:
                coord._heartbeat_looper.stop()
                break
            else:
                yield async_delay()

        coord2 = ConsumerGroup(
            self.client2,
            group,
            topics=[self.topic],
            processor=processor,
            session_timeout_ms=6000,
            retry_backoff_ms=100,
            heartbeat_interval_ms=1000,
            fatal_backoff_ms=3000,
            consumer_kwargs=dict(auto_commit_every_ms=1000),
        )
        coord2_start_d = coord2.start()
        self.addCleanup(coord2.stop)
        # FIXME: This doesn't seem to get fired reliably.
        coord2_start_d # noqa
        # self.addCleanup(lambda: coord2_start_d)

        # send some messages and see that they're processed
        # the commit will eventually fail because we're rebalancing
        for part in range(15):
            yield async_delay()
            values = yield self.send_messages(part % self.num_partitions, [part])
            msgs = yield record_stream.get()
            if msgs[0].partition != part:
                # once the commit fails, we will see the msg twice
                break
            self.assertEqual(msgs[0].message.value, values[0])

        yield wait_for_assignments(self.topic, self.num_partitions, [coord, coord2])

        # Once assignments have been received we need to ensure that the record
        # stream is clear of any duplicate messages. We do this by producing
        # a sentinel to each partition and consuming messages from the stream
        # until all the sentinels have appeared at least once. At that point
        # any churn should have cleared up and we can depend on lock-step
        # delivery.
        pending_sentinels = {}
        for part in range(self.num_partitions):
            [value] = yield self.send_messages(part, ["sentinel"])
            pending_sentinels[part] = value
        while pending_sentinels:
            [message] = yield record_stream.get()
            if pending_sentinels.get(message.partition) == message.message.value:
                del pending_sentinels[message.partition]

        # after the cluster has re-formed, send some more messages
        # and check that we get them too (and don't get the old messages again)
        record_stream = DeferredQueue(backlog=1)
        for part in range(self.num_partitions):
            yield async_delay()
            [value] = yield self.send_messages(part, [part])
            log.debug("waiting for messages from partition %d", part)
            [message] = yield record_stream.get()
            self.assertEqual(message.partition, part)
            self.assertEqual(message.message.value, value)
