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
Test code for KafkaClient class.
"""

import logging
import random
import struct
from itertools import cycle
from pprint import pformat
from unittest.mock import ANY, Mock

from .. import KafkaClient
from ..client import _normalize_hosts
from ..client import log as client_log
from ..common import (
    ApiVersion,
    ApiVersionResponse,
    BrokerMetadata,
    CoordinatorNotAvailable,
    FailedPayloadsError,
    FetchRequest,
    FetchResponse,
    KafkaUnavailableError,
    LeaderNotAvailableError,
    LeaderUnavailableError,
    NotCoordinator,
    NotLeaderForPartitionError,
    OffsetAndMessage,
    OffsetCommitRequest,
    OffsetCommitResponse,
    OffsetFetchRequest,
    OffsetFetchResponse,
    OffsetRequest,
    OffsetResponse,
    PartitionMetadata,
    PartitionUnavailableError,
    ProduceRequest,
    ProduceResponse,
    RequestTimedOutError,
    TopicAndPartition,
    TopicMetadata,
    UnknownError,
    UnknownTopicOrPartitionError,
    _LeaveGroupRequest,
    _LeaveGroupResponse,
)
from ..kafkacodec import KafkaCodec, create_message
from .endpoints import BlackholeEndpoint, Connections, FailureEndpoint
from .logtools import capture_logging
from .testutil import first
from twisted.internet.defer import Deferred
from twisted.internet.error import ConnectError, ConnectionLost, UserError
from twisted.internet.task import Clock
from twisted.python.failure import Failure
from twisted.test.proto_helpers import MemoryReactorClock
from twisted.trial import unittest

log = logging.getLogger(__name__)


def createMetadataResp():
    from .test_kafkacodec import create_encoded_metadata_response

    node_brokers = {
        0: BrokerMetadata(0, "brokers1.afkak.example.com", 1000),
        1: BrokerMetadata(1, "brokers1.afkak.example.com", 1001),
        3: BrokerMetadata(3, "brokers2.afkak.example.com", 1000),
    }

    topic_partitions = {
        "topic1": TopicMetadata(
            "topic1",
            0,
            {
                0: PartitionMetadata("topic1", 0, 0, 1, (0, 2), (2,)),
                1: PartitionMetadata("topic1", 1, 1, 3, (0, 1), (0, 1)),
            },
        ),
        "topic2": TopicMetadata(
            "topic2",
            0,
            {
                0: PartitionMetadata("topic2", 0, 0, 0, (), ()),
            },
        ),
        "topic3": TopicMetadata("topic3", 5, {}),
    }
    return create_encoded_metadata_response(node_brokers, topic_partitions)


def encode_metadata_response(brokers, topics):
    from .test_kafkacodec import create_encoded_metadata_response

    broker_map = {bm.node_id: bm for bm in brokers}
    return create_encoded_metadata_response(broker_map, topics)[4:]  # Slice off correlation ID


def find_coordinator_ok(node_id, host, port):
    """
    Encode a successful FindCoordinatorResponse v0.
    """
    host_bytes = host.encode("ascii")
    return b"".join(
        [  # FindCoordinator Response v0
            struct.pack(">h", 0),  # No error
            struct.pack(">i", node_id),  # Coordinator id
            struct.pack(">h", len(host_bytes)),  # The Coordinator host
            host_bytes,
            struct.pack(">i", port),  # The Coordinator port
        ]
    )


def find_coordinator_error(error):
    """
    Encode an error'd out FindCoordinatorResponse v0.

    :param error: Kafka errno
    """
    return b"".join(
        [  # FindCoordinator Response v0
            struct.pack(">h", error),  # Error Code
            struct.pack(">i", -1),  # Coordinator id
            struct.pack(">h", 0),  # The Coordinator host
            struct.pack(">i", 0),  # The Coordinator port
        ]
    )


class TestKafkaClient(unittest.TestCase):
    maxDiff = None

    many_brokers = [
        BrokerMetadata(node_id, host="broker_{}".format(node_id), port=node_id) for node_id in range(1001, 1011)
    ]

    testMetaData = createMetadataResp()

    def client_with_metadata(
        self,
        brokers,
        topic_partitions=None,
        topic_metadata=None,
        disconnect_on_timeout=False,
        enable_protocol_version_discovery=False,
    ):
        """
        :param brokers: Broker metadata to load
        :type brokers: List[BrokerMetadata]

        :param topic_partitions:
            Map of topic names to partition count. Partition numbers from 0..n
            will be assigned in the generated metadata. The replica and ISR
            lists for each partition will be empty.

            Mutually exclusive with *topic_metadata*.
        :type topics: Dict[str, int]

        :param topic_metadata:
            Full topic metadata as required by :func:`encode_metadata_response()`.

            Mutually exlusive with *topic_partitions*.

        :param bool disconnect_on_timeout:
            Passed on to KafkaClient.
        :param bool enable_protocol_version_discovery:
            Passed on to KafkaClient.
        """
        broker_id_seq = cycle(bm.node_id for bm in brokers)

        if topic_metadata is not None:
            assert topic_partitions is None
            topic_map = topic_metadata
        elif topic_partitions is None:
            topic_map = {}
        else:
            topic_map = {}
            for topic, partition_count in topic_partitions.items():
                node_id = next(broker_id_seq)
                topic_map[topic] = TopicMetadata(
                    topic,
                    0,
                    {
                        p: PartitionMetadata(
                            topic=topic,
                            partition=p,
                            partition_error_code=0,  # no error
                            leader=node_id,
                            replicas=(node_id,),
                            isr=(node_id,),
                        )
                        for p in range(partition_count)
                    },
                )

        reactor = Clock()
        connections = Connections()
        client = KafkaClient(
            hosts="bootstrap:9092",
            reactor=reactor,
            endpoint_factory=connections,
            disconnect_on_timeout=disconnect_on_timeout,
            enable_protocol_version_discovery=enable_protocol_version_discovery,
        )

        d = client.load_metadata_for_topics()
        conn = connections.accept("bootstrap")
        connections.flush()
        request = self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, ANY))
        request.respond(encode_metadata_response(brokers, topic_map))
        connections.flush()
        self.assertTrue(self.successResultOf(d))

        return reactor, connections, client

    def test_repr(self):
        c = KafkaClient("1.kafka.internal, 2.kafka.internal:9910", clientId="MyClient")
        self.assertEqual(
            ("<KafkaClient clientId=MyClient" " hosts=1.kafka.internal:9092 2.kafka.internal:9910" " timeout=10.0>"),
            repr(c),
        )

    def test_client_bad_timeout(self):
        self.assertRaises(
            Exception,
            KafkaClient,
            "kafka.example.com",
            clientId="MyClient",
            timeout="100ms",
        )
        self.assertRaises(
            TypeError,
            KafkaClient,
            "kafka.example.com",
            clientId="MyClient",
            timeout=None,
        )

    def test_client_bad_hosts(self):
        """
        `KafkaClient.__init__` raises an exception when passed an invalid
        *hosts* argument.
        """
        self.assertRaises(Exception, KafkaClient, hosts="foo:notaport")

    def test_update_cluster_hosts(self):
        c = KafkaClient(hosts="www.example.com")
        c.update_cluster_hosts("meep.org")
        self.assertEqual(c._bootstrap_hosts, [("meep.org", 9092)])

    def test_update_cluster_hosts_empty(self):
        """
        Attempting to set empty bootstrap hosts raises an exception. The
        configured hosts don't change.
        """
        c = KafkaClient(hosts=[("kafka.example.com", 1234)])
        self.assertRaises(Exception, c.update_cluster_hosts, ["foo:notaport"])
        self.assertEqual(c._bootstrap_hosts, [("kafka.example.com", 1234)])

    def test_send_broker_unaware_request_bootstrap_fail(self):
        """
        Broker unaware requests fail with `KafkaUnavailableError` when boostrap
        fails.

        This scenario makes two connection attempts in random order, one for
        each configured bootstrap host. Both fail.
        """
        client = KafkaClient(
            hosts=["kafka01:9092", "kafka02:9092"],
            reactor=MemoryReactorClock(),
            # Every connection attempt will immediately fail due to this
            # endpoint, including attempts to bootstrap.
            endpoint_factory=FailureEndpoint,
        )

        d = client.load_metadata_for_topics("sometopic")

        self.failureResultOf(d).trap(KafkaUnavailableError)

    def test_send_broker_unaware_request_brokers_fail(self):
        """
        Broker unaware requests fail with `KafkaUnavailableError` when none of
        the clients can answer requests and the fallback bootstrap also fails.

        In this scenario five connection attempts occur:

        1. A bootstrap request to get cluster metadata. This succeeds and
           returns topic metadata which references two brokers (we will call
           these "topic brokers").
        2. A request to a topic broker. We fail this by timeout.
        3. A request to the other topic broker. We fail this by timeout.
        4. A fallback bootstrap request to a bootstrap broker. We fail this.
        5. A fallback bootstrap request to the other bootstrap broker. We fail
           this.

        At this point all brokers have been tried so the request fails with
        a `KafkaUnavailableError`.
        """
        from .test_kafkacodec import create_encoded_metadata_response

        reactor = MemoryReactorClock()
        conns = Connections()
        client = KafkaClient(
            hosts="bootstrap",
            timeout=10 * 1000,  # 10 seconds in ms
            reactor=reactor,
            endpoint_factory=conns,
            # We want to hit the request timeout to test failover behavior, so
            # this retry policy returns a delay much longer than the timeout.
            retry_policy=lambda failures: 100.0,
        )
        topic_brokers = [
            BrokerMetadata(1, "broker1", 9092),
            BrokerMetadata(2, "broker2", 9092),
        ]

        # Bootstrap the client.
        bootstrap_d = client.load_metadata_for_topics("bootstrap_topic")
        self.assertNoResult(bootstrap_d)
        conn = conns.accept("bootstrap")
        conn.pump.flush()
        request = self.successResultOf(conn.server.requests.get())
        request.respond(
            create_encoded_metadata_response(
                {b.node_id: b for b in topic_brokers},
                {},  # No topic metadata.
            )[4:]
        )
        conn.pump.flush()
        self.assertTrue(self.successResultOf(bootstrap_d))
        self.assertEqual({1, 2}, set(client._brokers.keys()))

        # Now hit all the failure paths.
        client.update_cluster_hosts("bootstrap1,bootstrap2")
        d = client.load_metadata_for_topics("sometopic")
        self.assertNoResult(d)

        conns.fail("broker?", ConnectError())
        # The broker client will wait and retry until the request times out
        # (probably should change this for unaware requests) so advance the
        # clock up to the timeout.
        reactor.pump([1.0] * 10)
        self.assertNoResult(d)
        conns.accept("broker?").client.connectionLost(Failure(ConnectionLost()))
        reactor.pump([1.0] * 10)  # Again with the timeout.
        self.assertNoResult(d)

        # Now it falls back to the bootstrap hosts.
        conns.accept("bootstrap?").client.connectionLost(Failure(ConnectionLost()))
        self.assertNoResult(d)
        conns.fail("bootstrap?", ConnectError())

        self.failureResultOf(d).check(KafkaUnavailableError)

        self.successResultOf(client.close())

    def test_make_request_to_broker_alerts_when_blocked(self):
        """
        When the reactor is blocked the request timeout warning includes enough
        information to point at that issue: it shows that the timeout delayed
        call was invoked later than scheduled.
        """
        reactor, connections, client = self.client_with_metadata(
            brokers=[BrokerMetadata(0, "kafka", 9092)],
            topic_partitions={"topic": 1},
        )

        with capture_logging(client_log, logging.WARNING) as records:
            d = client.send_produce_request(
                [ProduceRequest("topic", 0, [create_message(b"a")])],
            )

            reactor.advance(client.timeout + 1)  # fire the timeout warning errback

        [record] = records
        self.assertEqual(record.args, (ANY, client.timeout, client.timeout + 1))

        self.failureResultOf(d)

    def test_make_request_to_broker_min_timeout(self):
        """
        Test that request timeouts can be overridden individually by the
        caller by passing the `min_timeout` argument.
        """
        cbArg = []

        def _recordCallback(res):
            # Record how the deferred returned by our mocked broker is called
            cbArg.append(res)
            return res

        d = Deferred().addBoth(_recordCallback)
        mock_broker = Mock()
        mocked_brokers = {("kafka31", 9092): mock_broker}
        # inject broker side effects
        mock_broker.makeRequest.return_value = d
        mock_broker.cancelRequest.side_effect = lambda rId, reason: d.errback(reason)

        reactor = MemoryReactorClock()
        client = KafkaClient(hosts="kafka31:9092", reactor=reactor, timeout=5000)

        # Alter the client's brokerclient dict to use our mocked broker
        client.clients = mocked_brokers
        client._collect_hosts_d = None
        respD = client._make_request_to_broker(mock_broker, 1, b"request", min_timeout=10.0)

        # Advance past the client timeout of 5000ms. The request should not
        # time out as the timeout has been overridden.
        reactor.advance(5.0)
        self.assertNoResult(respD)

        # Advance past the configured timeout. Now the request times out.
        reactor.pump(
            [
                4.0,  # Advance to the reactor block warning, clear it.
                1.0,  # Advance to the timeout.
            ]
        )
        self.failureResultOf(respD).check(RequestTimedOutError)

        # The timeout happened at the broker level, meaning it was cleared from
        # internal data structures.
        self.assertIs(None, cbArg[0].check(RequestTimedOutError))

    def test_load_metadata_for_topics(self):
        """
        A broker unaware request succeeds if at least one bootstrap host is
        available.
        """
        reactor = Clock()
        conns = Connections()
        client = KafkaClient(
            hosts="kafka1,kafka2,kafka3",
            reactor=reactor,
            endpoint_factory=conns,
        )

        d = client.load_metadata_for_topics("topic1", "topic2", "topic3", "topic4")

        conns.fail("kafka?", ConnectError())
        conns.accept("kafka?").client.connectionLost(Failure(ConnectionLost()))
        conn = conns.accept("kafka?")
        conn.pump.flush()
        request = self.successResultOf(
            conn.server.expectRequest(
                api_key=KafkaCodec.METADATA_KEY,
                api_version=0,
                correlation_id=1,
            )
        )

        self.assertEqual(
            request.rest,
            (b"\x00\x00\x00\x04" b"\x00\x06topic1\x00\x06topic2\x00\x06topic3\x00\x06topic4"),
        )
        request.respond(
            encode_metadata_response(
                [
                    BrokerMetadata(1, "kafka1", 9092),
                    BrokerMetadata(2, "kafka2", 9092),
                    BrokerMetadata(3, "kafka3", 9092),
                ],
                {
                    "topic1": TopicMetadata(
                        "topic1",
                        0,
                        {
                            0: PartitionMetadata("topic1", 0, 0, 1, (0, 2), (2,)),
                            1: PartitionMetadata("topic1", 1, 1, 3, (0, 1), (0, 1)),
                        },
                    ),
                    "topic2": TopicMetadata(
                        "topic2",
                        0,
                        {
                            # A partition with no replicas or ISRs.
                            0: PartitionMetadata("topic2", 0, 0, 2, (), ()),
                        },
                    ),
                    "topic3": TopicMetadata("topic3", LeaderNotAvailableError.errno, {}),
                    # This topic has partitions which lack a leader.
                    "topic4": TopicMetadata(
                        "topic4",
                        UnknownError.errno,
                        {
                            0: PartitionMetadata("topic4", 0, 0, -1, [], []),
                            1: PartitionMetadata("topic4", 1, 0, -1, [], []),
                        },
                    ),
                },
            )
        )
        conn.pump.flush()
        self.assertTrue(self.successResultOf(d))

        self.assertEqual(
            client._brokers,
            {
                1: BrokerMetadata(1, "kafka1", 9092),
                2: BrokerMetadata(2, "kafka2", 9092),
                3: BrokerMetadata(3, "kafka3", 9092),
            },
        )
        self.assertEqual(
            client.topics_to_brokers,
            {
                TopicAndPartition("topic1", 0): BrokerMetadata(1, "kafka1", 9092),
                TopicAndPartition("topic1", 1): BrokerMetadata(3, "kafka3", 9092),
                TopicAndPartition("topic2", 0): BrokerMetadata(2, "kafka2", 9092),
                TopicAndPartition("topic4", 0): None,
                TopicAndPartition("topic4", 1): None,
            },
        )
        # TODO more assertions about datastructures

    def test_get_leader_for_partitions_loads_metadata(self):
        """
        Topic metadata is loaded if it is not available for the required topic.
        """
        T1 = "topic_name"
        leader = random.choice(self.many_brokers)
        reactor, connections, client = self.client_with_metadata(self.many_brokers)

        d = client._get_leader_for_partition(T1, 0)
        self.assertNoResult(d)  # Not cached.

        conn = connections.accept("broker_*")
        connections.flush()
        request = self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, ANY))
        request.respond(
            encode_metadata_response(
                [leader],
                {
                    T1: TopicMetadata(
                        T1,
                        0,
                        {
                            0: PartitionMetadata(
                                T1,
                                0,
                                0,
                                leader.node_id,
                                (leader.node_id,),
                                (leader.node_id,),
                            ),
                        },
                    ),
                },
            )
        )
        connections.flush()

        self.assertEqual(
            {TopicAndPartition(topic=T1, partition=0): leader},
            client.topics_to_brokers,
        )
        self.assertEqual(leader, self.successResultOf(d))

    def test_get_leader_for_unassigned_partitions(self):
        """
        _get_partition_leader fails with PartitionUnavailableError when no
        partition is defined for a topic after attempting a metadata reload.
        """
        topic_metadata = {
            "topic_no_partitions": TopicMetadata("topic_no_partitions", 0, {}),
        }
        reactor, connections, client = self.client_with_metadata(self.many_brokers, topic_metadata=topic_metadata)

        self.assertDictEqual({}, client.topics_to_brokers)

        key = TopicAndPartition("topic_no_partitions", 0)
        eFail = PartitionUnavailableError("{} not available".format(key))

        d = client._get_leader_for_partition("topic_no_partitions", 0)

        # Same old, same old...
        conn = connections.accept("broker_*")
        connections.flush()
        request = self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, ANY))
        request.respond(encode_metadata_response([], topic_metadata))
        connections.flush()

        fail1 = self.failureResultOf(d, PartitionUnavailableError)
        # Make sure the error msg is correct
        self.assertEqual(type(eFail), fail1.type)
        self.assertEqual(eFail.args, fail1.value.args)

    def test_get_leader_returns_none_when_noleader(self):
        """
        _get_leader_for_partition tries to reload the metadata for a topic when
        it doesn't know of a leader. When the fresh data contains no leader it
        returns `None`.
        """
        topic_metadata = {
            "topic_noleader": TopicMetadata(
                "topic_noleader",
                0,
                {
                    # -1 means "no leader"
                    0: PartitionMetadata("topic_noleader", 0, 0, -1, [], []),
                    1: PartitionMetadata("topic_noleader", 1, 0, -1, [], []),
                },
            ),
        }
        reactor, connections, client = self.client_with_metadata(self.many_brokers, topic_metadata=topic_metadata)
        self.assertEqual(
            {
                TopicAndPartition("topic_noleader", 0): None,
                TopicAndPartition("topic_noleader", 1): None,
            },
            client.topics_to_brokers,
        )

        d = client._get_leader_for_partition("topic_noleader", 0)

        # Same old, same old...
        conn = connections.accept("broker_*")
        connections.flush()
        request = self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, ANY))
        request.respond(encode_metadata_response([], topic_metadata))
        connections.flush()

        self.assertIs(None, self.successResultOf(d))

    def test_get_leader_cached(self):
        """
        When the metadata for a topic is cached the partition leader is
        returned based on that. No network activity is required.
        """
        broker_0 = self.many_brokers[0].node_id
        broker_1 = self.many_brokers[1].node_id
        reactor, connections, client = self.client_with_metadata(
            self.many_brokers,
            topic_metadata={
                "topic": TopicMetadata(
                    "topic",
                    0,
                    {
                        0: PartitionMetadata(
                            "topic",
                            0,
                            0,
                            broker_0,
                            (broker_0, broker_1),
                            (broker_0, broker_1),
                        ),
                        1: PartitionMetadata(
                            "topic",
                            1,
                            0,
                            broker_1,
                            (broker_1, broker_0),
                            (broker_1, broker_0),
                        ),
                    },
                ),
            },
        )

        self.assertEqual(
            self.many_brokers[0],
            self.successResultOf(client._get_leader_for_partition("topic", 0)),
        )
        self.assertEqual(
            self.many_brokers[1],
            self.successResultOf(client._get_leader_for_partition("topic", 1)),
        )

    def test_send_produce_request_raises_when_noleader(self):
        """
        send_produce_request requires a partition leader. When none is found in
        the cached metadata it tries to reload the metadata. If there is still
        no leader, it fails with LeaderUnavailableError.
        """
        topic_metadata = {
            "topic_noleader": TopicMetadata(
                "topic_noleader",
                0,
                {
                    # -1 means "no leader"
                    0: PartitionMetadata("topic_noleader", 0, 0, -1, [], []),
                    1: PartitionMetadata("topic_noleader", 1, 0, -1, [], []),
                },
            ),
        }
        reactor, connections, client = self.client_with_metadata(self.many_brokers, topic_metadata=topic_metadata)

        d = client.send_produce_request(
            payloads=[
                ProduceRequest("topic_noleader", 0, [create_message(b"a"), create_message(b"b")]),
            ]
        )

        # Return metadata that still has no leader.
        conn = connections.accept("broker_*")
        connections.flush()
        request = self.successResultOf(conn.server.expectRequest(KafkaCodec.METADATA_KEY, 0, ANY))
        request.respond(encode_metadata_response(self.many_brokers, topic_metadata))
        connections.flush()

        self.failureResultOf(d, LeaderUnavailableError)

    def test_get_brokerclient(self):
        """
        _get_brokerclient generates _KafkaBrokerClient objects based on cached
        broker metadata.
        """
        reactor, connections, client = self.client_with_metadata(
            [
                BrokerMetadata(1, "broker_1", 4567),
                BrokerMetadata(2, "broker_2", 9092),
                BrokerMetadata(3, "broker_3", 45678),
            ]
        )

        b1 = client._get_brokerclient(1)
        self.assertEqual("broker_1", b1.host)
        self.assertEqual(4567, b1.port)

        # The same broker client is returned each time.
        b2 = client._get_brokerclient(2)
        self.assertIs(b2, client._get_brokerclient(2))

        # Get another one...
        b3 = client._get_brokerclient(3)
        self.assertEqual(3, b3.node_id)
        self.assertEqual("broker_3", b3.host)
        self.assertEqual(45678, b3.port)

        self.assertIs(b1, client._get_brokerclient(1))

        self.assertRaises(KeyError, client._get_brokerclient, 4)

    def test_reset_topic_metadata(self):
        """
        `reset_topic_metadata()` removes topic metadata from the cache
        stored in these attributes:

        - `topic_partitions` — topic name to list of partition numbers
        - `topics_to_brokers` — TopicPartition tuple to BrokerMetadata
        - `topic_errors` — topic name to errno (or 0)
        """
        # Setup the client with the metadata we want start with
        brokers = [
            BrokerMetadata(node_id=1, host="kafka01", port=9092),
            BrokerMetadata(node_id=2, host="kafka02", port=9092),
        ]
        reactor, connections, client = self.client_with_metadata(
            brokers=brokers,
            topic_metadata={
                "Topic1": TopicMetadata(
                    "Topic1",
                    0,
                    {
                        0: PartitionMetadata(
                            topic="Topic1",
                            partition=0,
                            partition_error_code=0,
                            leader=1,
                            replicas=(1,),
                            isr=(1,),
                        ),
                    },
                ),
                "Topic2": TopicMetadata(
                    "Topic2",
                    0,
                    {
                        0: PartitionMetadata(
                            topic="Topic2",
                            partition=0,
                            partition_error_code=0,
                            leader=1,
                            replicas=(1,),
                            isr=(1,),
                        ),
                    },
                ),
                "Topic3": TopicMetadata(
                    "Topic3",
                    0,
                    {
                        part: PartitionMetadata(
                            topic="Topic3",
                            partition=part,
                            partition_error_code=0,
                            leader=brokers[part % len(brokers)].node_id,
                            replicas=(1, 2),
                            isr=(1, 2),
                        )
                        for part in range(4)
                    },
                ),
            },
        )

        # Check if we can get the error code.
        self.assertEqual(0, client.metadata_error_for_topic("Topic1"))

        # Reset the client's metadata for Topic2
        client.reset_topic_metadata("Topic2")

        # No change to brokers...
        # topics_to_brokers gets every (topic,partition) tuple removed
        # for that topic
        expected_topics_to_brokers = {
            TopicAndPartition("Topic1", 0): brokers[0],
            TopicAndPartition("Topic3", 0): brokers[0],
            TopicAndPartition("Topic3", 1): brokers[1],
            TopicAndPartition("Topic3", 2): brokers[0],
            TopicAndPartition("Topic3", 3): brokers[1],
        }
        self.assertEqual(expected_topics_to_brokers, client.topics_to_brokers)

        # topic_paritions gets topic deleted
        expected_topic_partitions = {"Topic1": [0], "Topic3": [0, 1, 2, 3]}
        self.assertEqual(expected_topic_partitions, client.topic_partitions)

        expected_topic_errors = {"Topic1": 0, "Topic3": 0}
        self.assertEqual(expected_topic_errors, client.topic_errors)

        # Resetting an unknown topic has no effect
        client.reset_topic_metadata("bogus")
        self.assertEqual(expected_topics_to_brokers, client.topics_to_brokers)
        self.assertEqual(expected_topic_partitions, client.topic_partitions)
        self.assertEqual(expected_topic_errors, client.topic_errors)

        # Resetting with no topics has no effect (no, this is not consistent
        # with load_metadata_for_topics()...)
        client.reset_topic_metadata()
        self.assertEqual(expected_topics_to_brokers, client.topics_to_brokers)
        self.assertEqual(expected_topic_partitions, client.topic_partitions)
        self.assertEqual(expected_topic_errors, client.topic_errors)

    def test_reset_topic_metadata_errors(self):
        """
        `reset_topic_metadata()` clears any topic error code. This is reflected
        in the `topic_errors` dict and the return value of
        `metadata_error_for_topic()`.
        """
        brokers = [
            BrokerMetadata(node_id=1, host="kafka01", port=9092),
            BrokerMetadata(node_id=2, host="kafka02", port=9092),
        ]
        reactor, connections, client = self.client_with_metadata(
            brokers=brokers,
            topic_metadata={
                "Topic2": TopicMetadata(
                    "Topic2",
                    0,
                    {
                        0: PartitionMetadata(
                            topic="Topic2",
                            partition=0,
                            partition_error_code=0,
                            leader=2,
                            replicas=(1, 2),
                            isr=(1, 2),
                        ),
                    },
                ),
                "Topic3": TopicMetadata("Topic3", UnknownTopicOrPartitionError.errno, {}),
                "Topic4": TopicMetadata(
                    "Topic4",
                    LeaderNotAvailableError.errno,
                    {
                        0: PartitionMetadata(
                            topic="Topic4",
                            partition=0,
                            partition_error_code=0,
                            leader=1,
                            replicas=(1,),
                            isr=(1,),
                        ),
                    },
                ),
                "Topic5": TopicMetadata("Topic5", LeaderNotAvailableError.errno, {}),
            },
        )

        # Setup the client with the metadata we want start with
        log.debug("topic_partitions = %s", pformat(client.topic_partitions))
        log.debug("topics_to_brokers = %s", pformat(client.topics_to_brokers))
        log.debug("topic_errors = %s", pformat(client.topic_errors))

        self.assertEqual(
            {
                "Topic2": 0,
                "Topic3": UnknownTopicOrPartitionError.errno,
                "Topic4": LeaderNotAvailableError.errno,
                "Topic5": LeaderNotAvailableError.errno,
            },
            client.topic_errors,
        )

        self.assertEqual(
            client.metadata_error_for_topic("Topic4"),
            LeaderNotAvailableError.errno,
        )
        client.reset_topic_metadata("Topic4")
        self.assertEqual(
            client.metadata_error_for_topic("Topic4"),
            UnknownTopicOrPartitionError.errno,
        )

        # Topic 5 has no partitions, so it _only_ appears in topic_errors.
        self.assertEqual(
            client.metadata_error_for_topic("Topic5"),
            LeaderNotAvailableError.errno,
        )
        client.reset_topic_metadata("Topic5")
        self.assertEqual(
            client.metadata_error_for_topic("Topic5"),
            UnknownTopicOrPartitionError.errno,
        )

        self.assertEqual(
            {
                "Topic2": 0,
                "Topic3": UnknownTopicOrPartitionError.errno,
            },
            client.topic_errors,
        )

        client.reset_all_metadata()
        self.assertEqual({}, client.topic_errors)

    def test_has_metadata_for_topic(self):
        reactor, connections, client = self.client_with_metadata(
            brokers=[BrokerMetadata(node_id=1, host="kafka01", port=9092)],
            topic_partitions={"Topic1": 1, "Topic2": 1, "Topic3": 4},
        )

        log.debug("topic_partitons = %r", client.topic_partitions)
        self.assertTrue(client.has_metadata_for_topic("Topic1"))
        self.assertTrue(client.has_metadata_for_topic("Topic2"))
        self.assertTrue(client.has_metadata_for_topic("Topic3"))
        self.assertFalse(client.has_metadata_for_topic("Unknown"))

    def test_reset_all_metadata(self):
        brokers = [
            BrokerMetadata(node_id=1, host="kafka01", port=9092),
            BrokerMetadata(node_id=2, host="kafka02", port=9092),
        ]
        reactor, connections, client = self.client_with_metadata(
            brokers,
            topic_partitions={"Topic1": 1, "Topic2": 1, "Topic3": 4},
        )

        # FIXME: Patch this in the client with the metadata we want start with
        client._group_to_coordinator = {
            "ConsumerGroup1": brokers[1],
        }

        self.assertTrue(client.has_metadata_for_topic("Topic1"))
        self.assertTrue(client.has_metadata_for_topic("Topic2"))
        self.assertTrue(client.has_metadata_for_topic("Topic3"))
        self.assertFalse(client.has_metadata_for_topic("Unknown"))

        client.reset_all_metadata()

        self.assertEqual(client.topics_to_brokers, {})
        self.assertEqual(client.topic_partitions, {})
        self.assertEqual(client.consumer_group_to_brokers, {})

    def test_client_close(self):
        """
        close() terminates any outstanding broker connections, ignoring any
        errors.
        """
        reactor, connections, client = self.client_with_metadata(
            brokers=[
                BrokerMetadata(1, "kafka0", 9092),
                BrokerMetadata(2, "kafka2", 9092),
            ]
        )

        request_d = client.load_metadata_for_topics()
        conn1 = connections.accept("kafka*")
        reactor.advance(client.timeout)  # Time out first attempt, try second broker.
        self.assertNoResult(request_d)
        conn2 = connections.accept("kafka*")
        conn2.client.transport.disconnectReason = UserError()

        close_d = client.close()
        self.assertNoResult(close_d)  # Waiting for connection shutdown.

        connections.flush()  # Complete connection shutdown.
        self.assertIs(None, self.successResultOf(close_d))
        self.assertTrue(conn1.server.transport.disconnected)
        self.assertTrue(conn2.server.transport.disconnected)
        # FIXME Afkak #71: The bootstrap retry loop leaves delayed calls (for timeouts)
        # in the reactor.
        self.assertEqual([], reactor.getDelayedCalls())

    def test_client_close_no_clients(self):
        client = KafkaClient(
            hosts="kafka",
            reactor=Clock(),
            endpoint_factory=BlackholeEndpoint,
        )
        self.successResultOf(client.close())
        self.assertEqual([], client.reactor.getDelayedCalls())

    def test_client_close_poisons(self):
        """
        Once close() has been called the client cannot be used to make further
        requests.
        """
        reactor, connections, client = self.client_with_metadata(
            brokers=[
                BrokerMetadata(1, "kakfa1", 9092),
            ]
        )

        self.successResultOf(client.close())

        self.failureResultOf(client.load_metadata_for_topics())
        # FIXME Afkak #71: Reject new requests with a clean exception. We
        # currently get a KafkaUnavailableError that wraps an AttributeError
        # due to self.clients being None.
        # self.failureResultOf(client.load_metadata_for_topics(), ClientError)

    def test_client_close_during_metadata_load(self):
        reactor = Clock()
        client = KafkaClient(
            reactor=reactor,
            hosts="kafka",
            # Every connection attempt hangs forever.
            endpoint_factory=BlackholeEndpoint,
        )
        load_d = client.load_metadata_for_topics()
        self.assertNoResult(load_d)

        self.assertIs(None, self.successResultOf(client.close()))
        self.assertEqual([], reactor.getDelayedCalls())

        # XXX: This API doesn't make sense. This deferred should fail with an
        # exception like ClientClosed, not succeed as if metadata has loaded.
        # FIXME: Afkak #71: This doesn't actually fail, though it should.
        # self.assertIs(None, self.successResultOf(load_d))

    def test_load_consumer_metadata_for_group(self):
        """
        Test that a subsequent load request for the same group before the
        the previous one has completed does not make a new request, and that
        a request for a different group does make a new request. Also, check
        That once the request completes for 'Group1', that subsequent requests
        will make a new request.
        """
        G1 = "ConsumerGroup1"
        G2 = "ConsumerGroup2"
        response = b"".join(
            [  # FindCoordinator Response v0
                struct.pack(">h", 0),  # Error Code
                struct.pack(">i", 1001),  # Coordinator id
                struct.pack(">h", len(b"host1")),
                b"host1",  # The Coordinator host
                struct.pack(">i", 9092),  # The Coordinator port
            ]
        )
        reactor, connections, client = self.client_with_metadata(brokers=[])

        load1_d = client.load_consumer_metadata_for_group(G1)
        load2_d = client.load_consumer_metadata_for_group(G2)
        load3_d = client.load_consumer_metadata_for_group(G1)
        self.assertNoResult(load1_d)
        self.assertNoResult(load2_d)
        self.assertNoResult(load3_d)

        # Now 'send' a response to the first/3rd requests
        conn = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(response)
        connections.flush()

        # The G1 requests completed and the client's consumer metadata was updated.
        self.assertTrue(self.successResultOf(load1_d))
        self.assertNoResult(load2_d)
        self.assertTrue(self.successResultOf(load3_d))
        self.assertEqual(
            {
                "ConsumerGroup1": BrokerMetadata(node_id=1001, host="host1", port=9092),
            },
            client.consumer_group_to_brokers,
        )

        # Once a response has been received new calls trigger a fresh request.
        load4_d = client.load_consumer_metadata_for_group(G1)
        self.assertNoResult(load4_d)
        # The request goes to the host returned in the first request rather
        # than the boostrap host. This verifies that the broker metatdata was
        # merged into the client cache.
        connections.accept("host1")

    def test_load_consumer_metadata_for_group_failure(self):
        """
        A network-level error when fetching consumer metadata is transformed
        into a NoCoordinator failure.
        """
        G1 = "ConsumerGroup1"
        response = b"".join(
            [  # FindCoordinator Response v0
                struct.pack(">h", 15),  # Error Code
                struct.pack(">i", -1),  # Coordinator id
                struct.pack(">h", len(b"")),
                b"",  # The Coordinator host
                struct.pack(">i", -1),  # The Coordinator port
            ]
        )
        reactor, connections, client = self.client_with_metadata(brokers=[])

        load_d = client.load_consumer_metadata_for_group(G1)

        # Now 'send' a response to the request
        conn = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(response)
        connections.flush()

        # The G1 requests completed and the client's consumer metadata was updated.
        self.failureResultOf(load_d, CoordinatorNotAvailable)

    def test_load_consumer_metadata_for_group_unavailable(self):
        """
        A response that indicates no cooordinator is available causes the
        metadata fetch to fail with CoordinatorNotAvailable.
        """
        G1 = "ConsumerGroup1"
        response = b"".join(
            [  # FindCoordinator Response v0
                struct.pack(">h", CoordinatorNotAvailable.errno),
                struct.pack(">i", -1),  # Coordinator id
                struct.pack(">h", len(b"")),
                b"",  # The Coordinator host
                struct.pack(">i", -1),  # The Coordinator port
            ]
        )
        reactor, connections, client = self.client_with_metadata(brokers=[])

        load1_d = client.load_consumer_metadata_for_group(G1)
        load2_d = client.load_consumer_metadata_for_group(G1)
        self.assertNoResult(load1_d)
        self.assertNoResult(load2_d)

        # Now 'send' a response to the requests
        conn = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(response)
        connections.flush()

        # The G1 requests completed and the client's consumer metadata was updated.
        self.failureResultOf(load1_d, CoordinatorNotAvailable)
        self.failureResultOf(load2_d, CoordinatorNotAvailable)

    def test_send_produce_request(self):
        """
        send_produce_request routes payloads to the correct Kafka broker. This
        test covers several scenarios:

        1. Regular produce. The deferred fires once the broker responds.
        2. Produce with acks=0. No response is expected, so the deferred fires
           once the request has been written to the OS socket buffer.
        3. A regular produce that results in a NotLeaderForPartiton error. This
           has the side effect of clearing the topic's cached metadata.
        4. Another regular produce. Due to missing metadata a metadata request
           goes out first, then the actual requests.

        This should really be several separate tests.
        """
        T1 = "Topic1"
        T2 = "Topic2"

        connections = Connections()
        client = KafkaClient(
            hosts="kafka31:9092,kafka32:9092",
            reactor=Clock(),
            endpoint_factory=connections,
        )

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host="kafka31", port=9092),
            BrokerMetadata(node_id=2, host="kafka32", port=9092),
        ]
        client._brokers.update({bm.node_id: bm for bm in brokers})
        client.topic_partitions = {
            T1: [0],
            T2: [0],
        }
        client.topics_to_brokers = {
            TopicAndPartition(topic=T1, partition=0): brokers[0],
            TopicAndPartition(topic=T2, partition=0): brokers[1],
        }

        # Setup the payloads
        payloads = [
            ProduceRequest(
                topic=T1,
                partition=0,
                messages=[create_message("{} message {}".format(T1, i).encode("ascii")) for i in range(10)],
            ),
            ProduceRequest(
                topic=T2,
                partition=0,
                messages=[create_message("{} message {}".format(T2, i).encode("ascii")) for i in range(5)],
            ),
        ]

        respD = client.send_produce_request(payloads)

        # Accept the connection attemtps triggered by the request.
        conn31 = connections.accept("kafka31")
        conn32 = connections.accept("kafka32")

        # Dummy up some responses, one from each broker
        resp0 = struct.pack(">ih%dsiihq" % (len(T1)), 1, len(T1), T1.encode(), 1, 0, 0, 10)
        resp1 = struct.pack(">ih%dsiihq" % (len(T2)), 1, len(T2), T2.encode(), 1, 0, 0, 20)

        connections.flush()
        self.successResultOf(conn31.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY)).respond(resp0)
        self.successResultOf(conn32.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY)).respond(resp1)
        connections.flush()

        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(results, [ProduceResponse(T1, 0, 0, 10), ProduceResponse(T2, 0, 0, 20)])

        respD = client.send_produce_request(payloads, acks=0)
        connections.flush()

        # These requests succeed as soon as they are written to a socket because of acks=0.
        self.assertEqual([], list(self.successResultOf(respD)))

        # Read the requests on the server side.
        self.successResultOf(conn31.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY))
        self.successResultOf(conn32.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY))

        # And again, this time with an error coming back...
        respD = client.send_produce_request(payloads)
        connections.flush()

        # Dummy up some responses, one from each broker
        resp0 = struct.pack(">ih%dsiihq" % len(T1), 1, len(T1), T1.encode(), 1, 0, 0, 10)
        resp1 = struct.pack(
            ">ih%dsiihq" % len(T2),
            1,
            len(T2),
            T2.encode(),
            1,
            0,
            NotLeaderForPartitionError.errno,
            20,
        )
        self.successResultOf(conn31.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY)).respond(resp0)
        self.successResultOf(conn32.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY)).respond(resp1)
        connections.flush()

        self.failureResultOf(respD, NotLeaderForPartitionError)
        # *Only* the error'd topic's metadata was reset.
        self.assertTrue(client.has_metadata_for_topic(T1))
        self.assertFalse(client.has_metadata_for_topic(T2))

        # And again, this time with an error coming back...but ignored,
        # and a callback to pre-process the response
        def preprocCB(response):
            return response

        respD = client.send_produce_request(payloads, fail_on_error=False, callback=preprocCB)
        connections.flush()

        # Handle the metadata request. It may have gone to either broker.
        request = self.successResultOf(
            first(
                [
                    conn31.server.requests.get(),
                    conn32.server.requests.get(),
                ]
            )
        )
        request.respond(
            encode_metadata_response(
                brokers,
                {T2: TopicMetadata(T2, 0, {0: PartitionMetadata(T2, 0, 0, 2, (1,), (1,))})},
            )
        )
        connections.flush()

        # Metadata has been updated:
        self.assertTrue(client.has_metadata_for_topic(T2))

        # Now the ProduceRequest requests will be sent:
        connections.flush()

        # Dummy up some responses, one from each broker
        resp0 = struct.pack(">ih%dsiihq" % len(T1), 1, len(T1), T1.encode(), 1, 0, 0, 10)
        resp1 = struct.pack(
            ">ih%dsiihq" % len(T2),
            1,
            len(T2),
            T2.encode(),
            1,
            0,
            NotLeaderForPartitionError.errno,
            20,
        )
        self.successResultOf(conn31.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY)).respond(resp0)
        self.successResultOf(conn32.server.expectRequest(KafkaCodec.PRODUCE_KEY, 0, ANY)).respond(resp1)
        connections.flush()

        results = list(self.successResultOf(respD))
        self.assertEqual(results, [ProduceResponse(T1, 0, 0, 10), ProduceResponse(T2, 0, 6, 20)])

    def test_send_produce_request_timeout(self):
        """
        send_produce_request fails with a FailedPayloadsError when payloads are
        sent, but fail. That failure wraps the details of the underlying
        failures.
        """
        reactor, connections, client = self.client_with_metadata(
            brokers=[BrokerMetadata(0, "kafka", 9092)],
            topic_partitions={"topic": 1},
        )
        send_payload = ProduceRequest("topic", 0, [create_message(b"a")])

        d = client.send_produce_request([send_payload])
        reactor.advance(client.timeout)  # Time out the request.

        f = self.failureResultOf(d, FailedPayloadsError)
        self.assertEqual([], f.value.responses)
        [[failed_payload, failure]] = f.value.failed_payloads
        self.assertIs(send_payload, failed_payload)
        failure.trap(RequestTimedOutError)

    def test_send_fetch_request(self):
        """
        Test send_fetch_request
        """
        T1 = "Topic41"
        T2 = "Topic42"

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host="kafka41", port=9092),
            BrokerMetadata(node_id=2, host="kafka42", port=9092),
        ]
        reactor, connections, client = self.client_with_metadata(
            brokers=brokers,
            topic_partitions={T1: 1, T2: 1},
        )
        self.assertEqual(client.topic_partitions, {T1: [0], T2: [0]})
        # self.assertEqual(client.topics_to_brokers, {
        #     TopicAndPartition(topic=T1, partition=0): brokers[0],
        #     TopicAndPartition(topic=T2, partition=0): brokers[1],
        # })

        # Setup the payloads
        payloads = [FetchRequest(T1, 0, 0, 1024), FetchRequest(T2, 0, 0, 1024)]

        respD = client.send_fetch_request(payloads)

        # Dummy up some responses, the same from both brokers for simplicity
        msgs = [create_message(m) for m in [b"message1", b"hi", b"boo", b"foo", b"so fun!"]]
        ms1 = KafkaCodec._encode_message_set([msgs[0], msgs[1]], offset=0)
        ms2 = KafkaCodec._encode_message_set([msgs[2]], offset=0)
        ms3 = KafkaCodec._encode_message_set([msgs[3], msgs[4]], offset=48)

        # Note this includes a response for a topic/partition we didn't request
        # and further, that response has an error. However,
        # '_send_broker_aware_request' won't return it to us since it doesn't
        # match our request, so we don't error out.
        encoded = struct.pack(
            ">ih%dsiihqi%dsihqi%dsh%dsiihqi%ds" % (len(T1), len(ms1), len(ms2), len(T2), len(ms3)),
            2,  # Correlation ID, Num Topics
            len(T1),
            T1.encode(),
            2,
            0,
            0,
            10,
            len(ms1),
            ms1,
            1,
            1,
            20,
            len(ms2),
            ms2,  # Topic41, 2 partitions,
            # part0, no-err, high-water-mark-offset, msg1
            # part1, err=1, high-water-mark-offset, msg1
            len(T2),
            T2.encode(),
            1,
            0,
            0,
            30,
            len(ms3),
            ms3,
        )

        # 'send' the responses
        conn41 = connections.accept("kafka41")
        conn42 = connections.accept("kafka42")
        connections.flush()
        self.successResultOf(conn41.server.expectRequest(KafkaCodec.FETCH_KEY, 0, ANY)).respond(encoded)
        self.successResultOf(conn42.server.expectRequest(KafkaCodec.FETCH_KEY, 0, ANY)).respond(encoded)
        connections.flush()

        # check the results
        results = list(self.successResultOf(respD))

        def expand_messages(response):
            return FetchResponse(
                response.topic,
                response.partition,
                response.error,
                response.highwaterMark,
                list(response.messages),
            )

        expanded_responses = [expand_messages(r) for r in results]
        expect = [
            FetchResponse(
                T1,
                0,
                0,
                10,
                [OffsetAndMessage(0, msgs[0]), OffsetAndMessage(1, msgs[1])],
            ),
            FetchResponse(
                T2,
                0,
                0,
                30,
                [OffsetAndMessage(48, msgs[3]), OffsetAndMessage(49, msgs[4])],
            ),
        ]
        self.assertEqual(expect, expanded_responses)

        # Again, with a callback
        def preprocCB(response):
            return response

        respD = client.send_fetch_request(payloads, callback=preprocCB)

        # 'send' the responses
        connections.flush()
        self.successResultOf(conn41.server.expectRequest(KafkaCodec.FETCH_KEY, 0, ANY)).respond(encoded)
        self.successResultOf(conn42.server.expectRequest(KafkaCodec.FETCH_KEY, 0, ANY)).respond(encoded)
        connections.flush()
        # check the results
        results = list(self.successResultOf(respD))
        expanded_responses = [expand_messages(r) for r in results]
        expect = [
            FetchResponse(
                T1,
                0,
                0,
                10,
                [OffsetAndMessage(0, msgs[0]), OffsetAndMessage(1, msgs[1])],
            ),
            FetchResponse(
                T2,
                0,
                0,
                30,
                [OffsetAndMessage(48, msgs[3]), OffsetAndMessage(49, msgs[4])],
            ),
        ]
        self.assertEqual(expect, expanded_responses)

    def test_send_fetch_request_bad_timeout(self):
        client = KafkaClient(hosts="kafka41:9092,kafka42:9092")
        payload = [FetchRequest("T1", 0, 0, 1024)]
        timeout = client.timeout * 1000  # client.timeout=secs, max_wait=msecs
        d = client.send_fetch_request(payload, max_wait_time=timeout)
        self.failureResultOf(d, ValueError)

    def test_send_offset_request(self):
        """
        Test send_offset_request
        """
        T1 = "Topic51"
        T2 = "Topic52"

        connections = Connections()
        client = KafkaClient(
            hosts="kafka51:9092,kafka52:9092",
            reactor=Clock(),
            endpoint_factory=connections,
        )

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host="kafka51", port=9092),
            BrokerMetadata(node_id=2, host="kafka52", port=9092),
        ]
        client._brokers.update({bm.node_id: bm for bm in brokers})
        client.topic_partitions = {
            T1: [0],
            T2: [0],
        }
        client.topics_to_brokers = {
            TopicAndPartition(topic=T1, partition=0): brokers[0],
            TopicAndPartition(topic=T2, partition=0): brokers[1],
        }

        # Setup the payloads
        payloads = [
            OffsetRequest(T1, 0, -1, 20),  # ask for up to 20
            OffsetRequest(T2, 0, -2, 1),  # -2==earliest, only get 1
        ]

        respD = client.send_offset_request(payloads)

        # Dummy up some responses, one from each broker
        resp1 = b"".join(
            [
                # struct.pack(">i", 42),            # Correlation ID
                struct.pack(">i", 1),  # One topics
                struct.pack(">h", len(T1)),
                T1.encode(),  # First topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 0),  # Partition 0
                struct.pack(">h", 0),  # No error
                struct.pack(">i", 3),  # 3 offsets
                struct.pack(">q", 96),  # Offset 96
                struct.pack(">q", 98),  # Offset 98
                struct.pack(">q", 99),  # Offset 99
            ]
        )
        resp2 = b"".join(
            [
                # struct.pack(">i", 68),            # Correlation ID
                struct.pack(">i", 1),  # One topic
                struct.pack(">h", len(T2)),
                T2.encode(),  # First topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 0),  # Partition 0
                struct.pack(">h", 0),  # No error
                struct.pack(">i", 1),  # 1 offset
                struct.pack(">q", 4096),  # Offset 4096
            ]
        )

        # send the responses
        conn51 = connections.accept("kafka51")
        conn52 = connections.accept("kafka52")
        connections.flush()
        self.successResultOf(conn51.server.expectRequest(KafkaCodec.OFFSET_KEY, 0, ANY)).respond(resp1)
        self.successResultOf(conn52.server.expectRequest(KafkaCodec.OFFSET_KEY, 0, ANY)).respond(resp2)
        connections.flush()

        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(
            set(results),
            set(
                [
                    OffsetResponse(topic=T1, partition=0, error=0, offsets=(96, 98, 99)),
                    OffsetResponse(topic=T2, partition=0, error=0, offsets=(4096,)),
                ]
            ),
        )

        # Again, with a callback
        def preprocCB(response):
            return response

        respD = client.send_offset_request(payloads, callback=preprocCB)

        connections.flush()
        self.successResultOf(conn51.server.expectRequest(KafkaCodec.OFFSET_KEY, 0, ANY)).respond(resp1)
        self.successResultOf(conn52.server.expectRequest(KafkaCodec.OFFSET_KEY, 0, ANY)).respond(resp2)
        connections.flush()

        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(
            set(results),
            set(
                [
                    OffsetResponse(topic=T1, partition=0, error=0, offsets=(96, 98, 99)),
                    OffsetResponse(topic=T2, partition=0, error=0, offsets=(4096,)),
                ]
            ),
        )

    def test_send_offset_fetch_request(self):
        T1 = "Topic71"
        T2 = "Topic72"
        G1 = "ConsumerGroup1"
        reactor, connections, client = self.client_with_metadata(brokers=[])

        # Setup the payloads
        payloads = [
            OffsetFetchRequest(T1, 71),
            OffsetFetchRequest(T2, 72),
        ]

        # Dummy the response
        resp = b"".join(
            [
                struct.pack(">i", 2),  # Two topics
                struct.pack(">h", len(T1)),
                T1.encode(),  # First topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 71),  # Partition 71
                struct.pack(">q", 49),  # Offset 49
                struct.pack(">h", len(b"Metadata1")),
                b"Metadata1",  # Metadata
                struct.pack(">h", 0),  # No error
                struct.pack(">h", len(T2)),
                T2.encode(),  # Second topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 72),  # Partition 72
                struct.pack(">q", 27),  # Offset 27
                struct.pack(">h", len(b"Metadata2")),
                b"Metadata2",  # Metadata
                struct.pack(">h", 0),  # No error
            ]
        )

        respD = client.send_offset_fetch_request(G1, payloads)

        # That first lookup for the group should result in one call to
        # load_consumer_metadata_for_group
        conn1 = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn1.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(find_coordinator_ok(node_id=1, host="kafka99", port=9092))
        connections.flush()

        # 'send' the response
        conn2 = connections.accept("kafka99")
        connections.flush()
        req2 = self.successResultOf(conn2.server.expectRequest(KafkaCodec.OFFSET_FETCH_KEY, 1, ANY))
        req2.respond(resp)
        connections.flush()
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(
            set(results),
            set(
                [
                    OffsetFetchResponse(
                        topic=T1,
                        partition=71,
                        offset=49,
                        metadata=b"Metadata1",
                        error=0,
                    ),
                    OffsetFetchResponse(
                        topic=T2,
                        partition=72,
                        offset=27,
                        metadata=b"Metadata2",
                        error=0,
                    ),
                ]
            ),
        )

        # Again, with a callback (for full coverage)
        preproc_call_count = [0]

        def preprocCB(response):
            preproc_call_count[0] += 1
            return response

        respD = client.send_offset_fetch_request(G1, payloads, callback=preprocCB)

        # 'send' the responses
        connections.flush()
        req2 = self.successResultOf(conn2.server.expectRequest(KafkaCodec.OFFSET_FETCH_KEY, 1, ANY))
        req2.respond(resp)
        connections.flush()
        # check the results
        results = list(self.successResultOf(respD))
        # Test that the preprocessor callback was called (2 responses)
        self.assertEqual(preproc_call_count[0], 2)

        self.assertEqual(
            set(results),
            set(
                [
                    OffsetFetchResponse(
                        topic=T1,
                        partition=71,
                        offset=49,
                        metadata=b"Metadata1",
                        error=0,
                    ),
                    OffsetFetchResponse(
                        topic=T2,
                        partition=72,
                        offset=27,
                        metadata=b"Metadata2",
                        error=0,
                    ),
                ]
            ),
        )

    def test_send_offset_fetch_request_failure(self):
        """
        Test that when a request involving a consumer metadata broker fails
        that we reset the cached broker for the consumer group.
        """
        T1 = "Topic71"
        G1 = "ConsumerGroup1"
        reactor, connections, client = self.client_with_metadata(brokers=[])

        # Setup the payload
        payloads = [OffsetFetchRequest(T1, 78)]

        # Dummy the response
        resp = b"".join(
            [
                struct.pack(">i", 1),  # 1 topic
                struct.pack(">h", len(T1)),
                T1.encode(),  # Topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 78),  # Partition 78
                struct.pack(">q", -1),  # Offset -1
                struct.pack(">h", 0),  # Metadata
                struct.pack(">h", NotCoordinator.errno),
            ]
        )

        respD = client.send_offset_fetch_request(G1, payloads)

        # That first lookup for the group should result in one call to
        # load_consumer_metadata_for_group
        conn1 = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn1.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(find_coordinator_ok(node_id=1, host="kafka72", port=9092))
        connections.flush()

        # And the cache of the broker for the consumer group should be set
        self.assertIn(G1, client.consumer_group_to_brokers)

        # 'send' the response
        conn2 = connections.accept("kafka72")
        connections.flush()
        req2 = self.successResultOf(conn2.server.expectRequest(KafkaCodec.OFFSET_FETCH_KEY, 1, ANY))
        req2.respond(resp)
        connections.flush()

        # check the results
        self.assertTrue(self.failureResultOf(respD, NotCoordinator))
        self.assertNotIn(G1, client.consumer_group_to_brokers)
        # cleanup
        client.close()

    def test_send_offset_commit_request(self):
        """test_send_offset_commit_request"""
        T1 = "Topic61"
        T2 = "Topic62"
        G2 = "ConsumerGroup2"
        reactor, connections, client = self.client_with_metadata(brokers=[])

        # Setup the payloads
        respD = client.send_offset_commit_request(
            G2,
            payloads=[
                OffsetCommitRequest(T1, 61, 81, -1, b"metadata1"),
                OffsetCommitRequest(T2, 62, 91, -1, b"metadata2"),
            ],
        )

        # The coordinator didn't have any metadata for the group, so it tries to load some.
        conn1 = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn1.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G2.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(find_coordinator_ok(node_id=10, host="kafka123", port=1023))
        connections.flush()

        # Dummy up a response for the commit
        resp1 = b"".join(
            [
                struct.pack(">i", 2),  # Two topics
                struct.pack(">h", len(T1)),
                T1.encode(),  # First topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 61),  # Partition 61
                struct.pack(">h", 0),  # No error
                struct.pack(">h", len(T2)),
                T2.encode(),  # Second topic
                struct.pack(">i", 1),  # 1 partition
                struct.pack(">i", 62),  # Partition 62
                struct.pack(">h", 0),  # No error
            ]
        )

        # 'send' the responses
        conn2 = connections.accept("kafka123")
        connections.flush()
        req2 = self.successResultOf(conn2.server.expectRequest(KafkaCodec.OFFSET_COMMIT_KEY, 1, ANY))
        req2.respond(resp1)
        connections.flush()

        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(
            set(results),
            set(
                [
                    OffsetCommitResponse(topic=T1, partition=61, error=0),
                    OffsetCommitResponse(topic=T2, partition=62, error=0),
                ]
            ),
        )

    def test_send_offset_commit_request_failure(self):
        """
        When the Kafka broker is unavailable, that the proper
        CoordinatorNotAvailable is raised
        """
        T1 = "Topic61"
        G1 = "ConsumerGroup1"
        reactor, connections, client = self.client_with_metadata(brokers=[])

        # Setup the payloads
        d = client.send_offset_commit_request(
            group=G1,
            payloads=[
                OffsetCommitRequest(T1, 61, 81, -1, b"metadata1"),
            ],
        )

        # The coordinator didn't have any metadata for the group, so it tries to load some.
        conn1 = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn1.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(find_coordinator_error(CoordinatorNotAvailable.errno))
        connections.flush()

        self.failureResultOf(d, CoordinatorNotAvailable)

    def test_send_request_to_coordinator(self):
        """
        Test the _send_request_to_coordinator method and error handling
        """
        G1 = "group1"
        reactor, connections, client = self.client_with_metadata(brokers=[])

        # Setup the payloads, encoder & decoder funcs
        d = client._send_request_to_coordinator(
            group=G1,
            payload=_LeaveGroupRequest("group1", "member"),
            encoder_fn=KafkaCodec.encode_leave_group_request,
            decode_fn=KafkaCodec.decode_leave_group_response,
            min_timeout=15,
        )
        # Shouldn't have a result yet. If we do, there was an error
        self.assertNoResult(d)

        # The coordinator didn't have any metadata for the group, so it tries to load some.
        conn1 = connections.accept("bootstrap")
        connections.flush()
        req1 = self.successResultOf(conn1.server.expectRequest(KafkaCodec.FIND_COORDINATOR_KEY, 0, ANY))
        assert G1.encode() in req1.rest  # TODO: Better assert on the request instead of allowing ANY.
        req1.respond(find_coordinator_ok(node_id=1001, host="host1", port=9092))
        connections.flush()

        self.assertEqual(
            {
                "group1": BrokerMetadata(node_id=1001, host="host1", port=9092),
            },
            client.consumer_group_to_brokers,
        )

        conn2 = connections.accept("host1")
        connections.flush()
        req2 = self.successResultOf(conn2.server.expectRequest(KafkaCodec.LEAVE_GROUP_KEY, 0, ANY))
        # dummy response
        req2.respond(struct.pack(">ih", 9, 0))
        connections.flush()

        # check the result. Should be a success response
        self.assertEqual(_LeaveGroupResponse(0), self.successResultOf(d))

    def test_client_disconnect_on_timeout_false(self):
        """
        The connection to a broker is not terminated due to request timeout
        unless `disconnect_on_timeout` is set.
        """
        self._check_disconnect_on_timeout(False)

    def _check_disconnect_on_timeout(self, disconnect_on_timeout):
        reactor, connections, client = self.client_with_metadata(
            brokers=[BrokerMetadata(0, "kafka", 9092)],
            topic_partitions={"topic": 1},
            disconnect_on_timeout=disconnect_on_timeout,
        )
        payload = ProduceRequest("topic", 0, [create_message(b"a")])

        # Kick off a request to the client to trigger a connection attempt
        respD = client.send_produce_request([payload])

        # Accept the connection attempt, but don't respond.
        conn = connections.accept("kafka")
        connections.flush()

        # Cause that request to timeout
        reactor.advance(client.timeout * 0.91)  # fire the reactor-blocked
        reactor.advance(client.timeout)  # fire the timeout errback
        connections.flush()

        # Check that the request was timed out
        self.failureResultOf(respD)

        # Make sure we didn't try to disconnect
        self.assertEqual(disconnect_on_timeout, conn.client.transport.disconnected)

    def test_client_disconnect_on_timeout_true(self):
        """
        When a client request to a broker times out the connection to that
        broker is terminated.
        """
        self._check_disconnect_on_timeout(True)

    def test_client_topic_fully_replicated(self):
        """test_client_topic_fully_replicated"

        Test the happy path of client.topic_fully_replicated()
        """
        client = KafkaClient(
            hosts="kafka01:9092,kafka02:9092",
            reactor=Clock(),
            endpoint_factory=FailureEndpoint,
        )

        # Setup the client with the metadata we want start with
        client.topic_partitions = {
            "Topic0": [],  # No partitions.
            "Topic2": [0],  # Not in partition_meta below
            "Topic3": [0, 1, 2, 3],  # "Good" partition
        }
        client.partition_meta = {
            TopicAndPartition(topic="Topic3", partition=0): PartitionMetadata("Topic3", 0, 0, 1, [1, 2], [1, 2]),
            TopicAndPartition(topic="Topic3", partition=1): PartitionMetadata("Topic3", 1, 0, 2, [2, 1], [1, 2]),
            TopicAndPartition(topic="Topic3", partition=2): PartitionMetadata("Topic3", 2, 0, 1, [1, 2], [1, 2]),
            TopicAndPartition(topic="Topic3", partition=3): PartitionMetadata("Topic3", 3, 0, 2, [2, 1], [1, 2]),
        }

        # Topics which don't have partitions aren't 'fully replicated'
        self.assertFalse(client.topic_fully_replicated("Topic0"))
        # Topics which we don't have any metadata for aren't 'fully replicated'
        self.assertFalse(client.topic_fully_replicated("Topic1"))
        # Topics which are only partially represented aren't 'fully replicated'
        self.assertFalse(client.topic_fully_replicated("Topic2"))

        # Topic with all the partitions having equal replicas & ISRs ARE
        self.assertTrue(client.topic_fully_replicated("Topic3"))

    def test_get_api_versions(self):
        """
        The client can get the API versions supported by the brokers.
        """
        reactor, connections, client = self.client_with_metadata(brokers=[], enable_protocol_version_discovery=True)

        d = client.fetch_api_versions()
        self.assertNoResult(d)

        # Accept connection from the client
        conn = connections.accept("bootstrap")
        connections.flush()
        req = self.successResultOf(conn.server.expectRequest(KafkaCodec.API_VERSIONS_KEY, 0, ANY))

        api_versions = [(0, 0, 8), (1, 0, 11)]

        # Pack response bytes
        resp_bytes = struct.pack(">hi", 0, len(api_versions))
        for api_key, min_version, max_version in api_versions:
            resp_bytes += struct.pack(">hhh", api_key, min_version, max_version)

        req.respond(resp_bytes)

        connections.flush()

        # Check the received response
        result = self.successResultOf(d)
        self.assertEqual(result.error_code, 0)
        self.assertEqual(len(result.api_versions), len(api_versions))
        expected_resp = ApiVersionResponse(error_code=0, api_versions=[ApiVersion(*v) for v in api_versions])
        self.assertEqual(result, expected_resp)


class NormalizeHostsTests(unittest.TestCase):
    def test_bare_host_string(self):
        """
        The default port is inferred when none is given.
        """
        self.assertEqual([("kafka", 9092)], _normalize_hosts("kafka"))
        self.assertEqual([("127.0.0.1", 9092)], _normalize_hosts("127.0.0.1"))
        self.assertEqual([("kafka.internal", 9092)], _normalize_hosts("kafka.internal"))
        self.assertEqual(
            [("kafka-1.internal", 9092), ("kafka-2.internal", 9092)],
            _normalize_hosts(b"kafka-1.internal, kafka-2.internal"),
        )

    def test_string_with_ports(self):
        """
        The string may specify ports.
        """
        self.assertEqual([("kafka", 1234)], _normalize_hosts("kafka:1234 "))
        self.assertEqual(
            [("kafka", 1234), ("kafka", 2345)],
            _normalize_hosts("kafka:1234 ,kafka:2345"),
        )
        self.assertEqual([("1.2.3.4", 5555)], _normalize_hosts(b" 1.2.3.4:5555 "))

    def test_sequence(self):
        """
        The input may be a sequence of hostnames or host ports. The default
        port is implied when none is given.
        """
        self.assertEqual(
            [("kafka", 9092)],
            _normalize_hosts(["kafka", b"kafka", ("kafka", 9092)]),
        )
        self.assertEqual(
            [("kafka1", 9092), ("kafka2", 9092)],
            _normalize_hosts([("kafka2", 9092), ("kafka1", 9092)]),
        )
        self.assertEqual(
            [("kafka1", 1234), ("kafka2", 2345)],
            _normalize_hosts([("kafka2", "2345"), ("kafka1", b"1234")]),
        )

    def test_ipv6(self):
        """
        IPv6 addresses may be passed as part of a sequence.
        """
        self.assertEqual(
            [("2001:db8::1", 2345), ("::1", 1234)],
            _normalize_hosts([("2001:db8::1", "2345"), (b"::1", 1234)]),
        )
