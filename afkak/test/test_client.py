# -*- coding: utf-8 -*-
# Copyright (C) 2015 Cyan, Inc.

"""
Test code for KafkaClient(object) class.
"""

from __future__ import division, absolute_import

from functools import partial
from copy import copy
from twisted.trial import unittest
from twisted.internet.base import DelayedCall
from twisted.internet.defer import (
    Deferred, succeed, fail, setDebugging,
    )
from twisted.internet.error import ConnectionRefusedError
from twisted.names import dns
from twisted.test.proto_helpers import MemoryReactorClock

import struct
import logging

from mock import MagicMock, Mock, patch, ANY

from afkak import KafkaClient
from afkak.brokerclient import KafkaBrokerClient
from afkak.common import (
    ProduceRequest, ProduceResponse, FetchRequest, FetchResponse,
    OffsetRequest, OffsetResponse, OffsetCommitRequest, OffsetCommitResponse,
    OffsetFetchRequest, OffsetFetchResponse,
    BrokerMetadata, PartitionMetadata, TopicMetadata,
    RequestTimedOutError, TopicAndPartition, KafkaUnavailableError,
    DefaultKafkaPort, LeaderUnavailableError, PartitionUnavailableError,
    FailedPayloadsError, NotLeaderForPartitionError, OffsetAndMessage,
    UnknownTopicOrPartitionError, ConsumerCoordinatorNotAvailableError,
    NotCoordinatorForConsumerError,
)
from afkak.kafkacodec import (create_message, KafkaCodec)
from afkak.client import _collect_hosts, _get_IP_addresses

DEBUGGING = True
setDebugging(DEBUGGING)
DelayedCall.debug = DEBUGGING

log = logging.getLogger(__name__)
logging.basicConfig(
    format='%(asctime)s:%(name)s:' +
    '%(levelname)s:%(process)d:%(message)s',
    level=logging.DEBUG
    )


def createMetadataResp():
    from .test_kafkacodec import create_encoded_metadata_response
    node_brokers = {
        0: BrokerMetadata(0, "brokers1.afkak.example.com", 1000),
        1: BrokerMetadata(1, "brokers1.afkak.example.com", 1001),
        3: BrokerMetadata(3, "brokers2.afkak.example.com", 1000)
        }

    topic_partitions = {
        "topic1": TopicMetadata(
            'topic1', 0, {
                0: PartitionMetadata("topic1", 0, 0, 1, (0, 2), (2,)),
                1: PartitionMetadata("topic1", 1, 1, 3, (0, 1), (0, 1))
                }),
        "topic2": TopicMetadata(
            'topic2', 0, {
                0: PartitionMetadata("topic2", 0, 0, 0, (), ())
                }),
        "topic3": TopicMetadata('topic3', 5, {}),
        }
    encoded = create_encoded_metadata_response(
        node_brokers, topic_partitions)
    return encoded


def brkrAndReqsForTopicAndPartition(client, topic, part=0):
    """
    Helper function to dig out the outstanding request so we can
    "send" the response to it. (fire the deferred)
    """
    broker = client.topics_to_brokers[TopicAndPartition(topic, part)]
    brokerClient = client._get_brokerclient(broker.host, broker.port)
    return (brokerClient, brokerClient.requests)


class TestKafkaClient(unittest.TestCase):
    testMetaData = createMetadataResp()

    def getLeaderWrapper(self, c, *args, **kwArgs):
        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: succeed(self.testMetaData)):
            d = c._get_leader_for_partition(*args)

        if 'errs' not in kwArgs:
            return self.successResultOf(d)
        return self.failureResultOf(d, kwArgs['errs'])

    def test_repr(self):
        c = KafkaClient('kafka.example.com', clientId='MyClient')
        c.clients = {('kafka.example.com', 9092): None}
        self.assertEqual(c.__repr__(), "<KafkaClient clientId=MyClient "
                         "brokers=[('kafka.example.com', 9092)] timeout=10.0>")

    def test_update_cluster_hosts(self):
        c = KafkaClient(hosts='www.example.com')
        c.update_cluster_hosts('meep.org')
        self.assertEqual(c._hosts, 'meep.org')
        self.assertEqual(c._collect_hosts_d, True)

    def test_send_broker_unaware_request_fail(self):
        """
        test_send_broker_unaware_request_fail
        Tests that call fails when all hosts are unavailable
        """

        mocked_brokers = {
            ('kafka01', 9092): MagicMock(),
            ('kafka02', 9092): MagicMock()
        }

        # inject side effects (makeRequest returns deferreds that are
        # pre-failed with a timeout...
        mocked_brokers[
            ('kafka01', 9092)
            ].makeRequest.side_effect = lambda a, b: fail(
            RequestTimedOutError("kafka01 went away (unittest)"))
        mocked_brokers[
            ('kafka02', 9092)
            ].makeRequest.side_effect = lambda a, b: fail(
            RequestTimedOutError("Kafka02 went away (unittest)"))

        client = KafkaClient(hosts=['kafka01:9092', 'kafka02:9092'])
        # Alter the client's brokerclient dict
        client.clients = mocked_brokers
        client._collect_hosts_d = None
        # Get the deferred (should be already failed)
        fail1 = client._send_broker_unaware_request(
            1, 'fake request')
        # check it
        self.successResultOf(
            self.failUnlessFailure(fail1, KafkaUnavailableError))

        # Check that the proper calls were made
        for key, brkr in mocked_brokers.iteritems():
            brkr.makeRequest.assert_called_with(1, 'fake request')

    def test_send_broker_unaware_request(self):
        """
        test_send_broker_unaware_request
        Tests that call works when at least one of the host is available
        """
        mocked_brokers = {
            ('kafka21', 9092): MagicMock(),
            ('kafka22', 9092): MagicMock(),
            ('kafka23', 9092): MagicMock()
        }
        # inject broker side effects
        mocked_brokers[('kafka21', 9092)].makeRequest.side_effect = \
            RequestTimedOutError("kafka01 went away (unittest)")
        mocked_brokers[('kafka22', 9092)].makeRequest.return_value = \
            succeed('valid response')
        mocked_brokers[('kafka23', 9092)].makeRequest.side_effect = \
            RequestTimedOutError("kafka03 went away (unittest)")

        client = KafkaClient(hosts='kafka21:9092,kafka22:9092')

        # Alter the client's brokerclient dict
        client.clients = mocked_brokers
        client._collect_hosts_d = None
        resp = self.successResultOf(
            client._send_broker_unaware_request(1, 'fake request'))

        self.assertEqual('valid response', resp)
        mocked_brokers[
            ('kafka22', 9092)].makeRequest.assert_called_with(
                1, 'fake request')

    def test_make_request_to_broker_handles_timeout(self):
        """test_make_request_to_broker_handles_timeout
        Test that request timeouts are handled properly
        """
        cbArg = []

        def _recordCallback(_):
            # Record how the deferred returned by our mocked broker is called
            cbArg.append(_)
            return _

        d = Deferred().addBoth(_recordCallback)
        mocked_brokers = {('kafka31', 9092): MagicMock()}
        # inject broker side effects
        mocked_brokers[('kafka31', 9092)].makeRequest.return_value = d
        mocked_brokers[(
            'kafka31', 9092)].cancelRequest.side_effect = \
            lambda rId, reason: d.errback(reason)

        reactor = MemoryReactorClock()
        client = KafkaClient(hosts='kafka31:9092', reactor=reactor)

        # Alter the client's brokerclient dict to use our mocked broker
        client.clients = mocked_brokers
        client._collect_hosts_d = None
        respD = client._send_broker_unaware_request(1, 'fake request')
        reactor.advance(client.timeout + 1)  # fire the timeout errback
        self.successResultOf(
            self.failUnlessFailure(respD, KafkaUnavailableError))
        self.assertTrue(cbArg[0].check(RequestTimedOutError))

    @patch('afkak.client.KafkaCodec')
    def test_load_metadata_for_topics(self, kCodec):
        """
        test_load_metadata_for_topics
        Load metadata for all topics
        """
        brokers = {}
        brokers[0] = BrokerMetadata(1, 'broker_1', 4567)
        brokers[1] = BrokerMetadata(2, 'broker_2', 5678)

        topics = {}
        topics['topic_1'] = TopicMetadata(
            'topic_1', 0, {
                0: PartitionMetadata('topic_1', 0, 0, 1, [1, 2], [1, 2])
                })
        topics['topic_noleader'] = TopicMetadata(
            'topic_noleader', 0, {
                0: PartitionMetadata('topic_noleader', 0, 0, -1, [], []),
                1: PartitionMetadata('topic_noleader', 1, 0, -1, [], [])
                })
        topics['topic_no_partitions'] = TopicMetadata('topic_no_partitions',
                                                      0, {})
        topics['topic_3'] = TopicMetadata(
            'topic_3', 0, {
                0: PartitionMetadata('topic_3', 0, 0, 0, [0, 1], [0, 1]),
                1: PartitionMetadata('topic_3', 1, 0, 1, [1, 0], [1, 0]),
                2: PartitionMetadata('topic_3', 2, 0, 0, [0, 1], [0, 1])
                })
        kCodec.decode_metadata_response.return_value = (brokers, topics)
        kCodec.encode_metadata_request.return_value = MagicMock()

        d = Deferred()
        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: d) as mockMethod:
            client = KafkaClient(hosts=['broker_1:4567'], timeout=None)

            # Ask the client to load the metadata, which should call
            # patched function, returning 'd'
            d1 = client.load_metadata_for_topics()
            self.assertEqual(d, d1)
            reqId = client._next_id() - 1
            mockMethod.assert_called_once_with(
                reqId, kCodec.encode_metadata_request.return_value)

            # make sure subsequent calls for all topics returns
            # in-process deferred
            d2 = client.load_metadata_for_topics()
            self.assertEqual(d, d2)
            # And no additional request sent
            mockMethod.assert_called_once_with(
                reqId, kCodec.encode_metadata_request.return_value)

        d.callback(self.testMetaData)
        # Now that we've made the request succeed, make sure the
        # in-process deferred is cleared, and the metadata is setup
        self.assertEqual(client.load_metadata, None)
        self.assertDictEqual({
            TopicAndPartition('topic_1', 0): brokers[1],
            TopicAndPartition('topic_noleader', 0): None,
            TopicAndPartition('topic_noleader', 1): None,
            TopicAndPartition('topic_3', 0): brokers[0],
            TopicAndPartition('topic_3', 1): brokers[1],
            TopicAndPartition('topic_3', 2): brokers[0]},
            client.topics_to_brokers)

        # now test that the failure case is handled properly
        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: fail(
                KafkaUnavailableError("test_load_metadata_for_topics"))):
            d3 = client.load_metadata_for_topics()
            self.successResultOf(
                self.failUnlessFailure(d3, KafkaUnavailableError))

    @patch('afkak.client.KafkaCodec')
    def test_get_leader_for_partitions_reloads_metadata(self, kCodec):
        """
        test_get_leader_for_partitions_reloads_metadata
        Get leader for partitions reload metadata if it is not available
        """
        # Create a client to test
        client = KafkaClient(hosts=['broker_1:4567'], timeout=None)
        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=0, host='broker_1', port=4567),
            BrokerMetadata(node_id=1, host='broker_2', port=5678),
        ]
        client.topic_partitions = {}
        client.topics_to_brokers = {}
        # topic metadata is loaded but empty
        self.assertDictEqual({}, client.topics_to_brokers)

        T1 = 'topic_no_partitions'

        def fake_lmdft(self, *topics):
            client.topics_to_brokers = {
                TopicAndPartition(topic=T1, partition=0): brokers[0],
            }
            return succeed(None)

        with patch.object(KafkaClient, 'load_metadata_for_topics',
                          side_effect=fake_lmdft) as lmdft:
            # Call _get_leader_for_partition and ensure it
            # calls load_metadata_for_topics
            leader = client._get_leader_for_partition('topic_no_partitions', 0)
            self.assertEqual(brokers[0], self.successResultOf(leader))
            lmdft.assert_called_once_with('topic_no_partitions')

    @patch('afkak.client.KafkaCodec')
    def test_get_leader_for_unassigned_partitions(self, kCodec):
        """
        test_get_leader_for_unassigned_partitions
        Get leader raises if no partitions is defined for a topic
        """

        brokers = {}
        brokers[0] = BrokerMetadata(0, 'broker_1', 4567)
        brokers[1] = BrokerMetadata(1, 'broker_2', 5678)

        topics = {
            'topic_no_partitions': TopicMetadata(
                'topic_no_partitions', 0, {})
            }
        kCodec.decode_metadata_response.return_value = (brokers, topics)

        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: succeed(self.testMetaData)):
            client = KafkaClient(hosts=['broker_1:4567'], timeout=None)

        self.assertDictEqual({}, client.topics_to_brokers)

        key = TopicAndPartition('topic_no_partitions', 0)
        eFail = PartitionUnavailableError(
            "{} not available".format(str(key)))

        fail1 = self.getLeaderWrapper(client, 'topic_no_partitions', 0,
                                      errs=PartitionUnavailableError)
        # Make sure the error msg is correct
        self.assertEqual(type(eFail), fail1.type)
        self.assertEqual(eFail.args, fail1.value.args)

    @patch('afkak.client.KafkaCodec')
    def test_get_leader_returns_none_when_noleader(self, kCodec):
        """
        test_get_leader_returns_none_when_noleader
        Confirm that _get_leader_for_partition() returns None when
        the partiion has no leader.
        Test by creating a client, patch afkak.client.KafkaCodec to return
        our special metadata, patch _send_broker_unaware_request to avoid
        making a connection, and then confirm that None is returned.
        """

        brokers = {}
        brokers[0] = BrokerMetadata(0, 'broker_1', 4567)
        brokers[1] = BrokerMetadata(1, 'broker_2', 5678)

        topics = {}
        topics['topic_noleader'] = TopicMetadata(
            'topic_noleader', 0, {
                0: PartitionMetadata('topic_noleader', 0, 0, -1, [], []),
                1: PartitionMetadata('topic_noleader', 1, 0, -1, [], [])
                })
        kCodec.decode_metadata_response.return_value = (brokers, topics)

        client = KafkaClient(hosts=['broker_1:4567', 'broker_2:5678'],
                             timeout=None)

        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: succeed(
                              self.testMetaData)):
            client._get_leader_for_partition('topic_noleader', 0)
        self.assertDictEqual(
            {
                TopicAndPartition('topic_noleader', 0): None,
                TopicAndPartition('topic_noleader', 1): None
            },
            client.topics_to_brokers)

        self.assertIsNone(
            self.getLeaderWrapper(client, 'topic_noleader', 0))
        self.assertIsNone(
            self.getLeaderWrapper(client, 'topic_noleader', 1))

        topics['topic_noleader'] = TopicMetadata(
            'topic_noleader', 0, {
                0: PartitionMetadata(
                    'topic_noleader', 0, 0, 0, [0, 1], [0, 1]),
                1: PartitionMetadata(
                    'topic_noleader', 1, 0, 1, [1, 0], [1, 0])
                })
        kCodec.decode_metadata_response.return_value = (brokers, topics)
        self.assertEqual(
            brokers[0], self.getLeaderWrapper(client, 'topic_noleader', 0))
        self.assertEqual(
            brokers[1], self.getLeaderWrapper(client, 'topic_noleader', 1))

    @patch('afkak.client.KafkaCodec')
    def test_send_produce_request_raises_when_noleader(self, kCodec):
        """
        test_send_produce_request_raises_when_noleader
        Send producer request raises LeaderUnavailableError if
        leader is not available
        """

        brokers = {}
        brokers[0] = BrokerMetadata(0, 'broker_1', 4567)
        brokers[1] = BrokerMetadata(1, 'broker_2', 5678)

        topics = {}
        topics['topic_noleader'] = TopicMetadata(
            'topic_noleader', 0, {
                0: PartitionMetadata('topic_noleader', 0, 0, -1, [], []),
                1: PartitionMetadata('topic_noleader', 1, 0, -1, [], [])
                })
        kCodec.decode_metadata_response.return_value = (brokers, topics)

        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: succeed(self.testMetaData)):
            client = KafkaClient(hosts=['broker_1:4567'], timeout=None)

            # create a list of requests (really just one)
            requests = [
                ProduceRequest("topic_noleader", 0,
                               [create_message("a"), create_message("b")])]
            # Attempt to send it, and ensure the returned deferred fails
            # properly
            fail1 = client.send_produce_request(requests)
            self.successResultOf(
                self.failUnlessFailure(fail1, LeaderUnavailableError))

    @patch('afkak.client._get_IP_addresses')
    def test__collect_hosts__happy_path(self, IP_addresses):
        """
        test__collect_hosts__happy_path
        Test the _collect_hosts function in client.py
        """
        hosts = "localhost:1234,localhost"
        IP_addresses.return_value = ['localhost']
        result = self.successResultOf(_collect_hosts(hosts))
        self.assertEqual(set(result), set([
            ('localhost', 1234),
            ('localhost', 9092),
        ]))

    @patch('afkak.client._get_IP_addresses')
    def test__collect_hosts__does_not_resolve_host(self, IP_addresses):
        hosts = "..."
        IP_addresses.return_value = None
        result = self.successResultOf(_collect_hosts(hosts))
        self.assertEqual(set([]), set(result))

    @patch('afkak.client._get_IP_addresses')
    def test__collect_hosts_with_csv(self, IP_addresses):
        hosts = 'kafka01:9092,kafka02,kafka03:9092'
        IP_addresses.side_effect = [['kafka01'], ['kafka02'], ['kafka03']]
        result = self.successResultOf(_collect_hosts(hosts))
        self.assertEqual(set(result), set([
            ('kafka01', 9092),
            ('kafka02', 9092),
            ('kafka03', 9092)
        ]))

    @patch('afkak.client._get_IP_addresses')
    def test__collect_hosts_with_unicode_csv(self, IP_addresses):
        hosts = u'kafka01:9092,kafka02:9092,kafka03:9092'
        IP_addresses.side_effect = [['kafka01'], ['kafka02'], ['kafka03']]
        result = self.successResultOf(_collect_hosts(hosts))
        self.assertEqual(set(result), set([
            ('kafka01', 9092),
            ('kafka02', 9092),
            ('kafka03', 9092)
        ]))

    @patch('afkak.client._get_IP_addresses')
    def test__collect_hosts__string_list(self, IP_addresses):
        hosts = [
            'localhost:1234',
            'localhost',
        ]
        IP_addresses.return_value = ['localhost']
        result = self.successResultOf(_collect_hosts(hosts))
        self.assertEqual(set(result), set([
            ('localhost', 1234),
            ('localhost', 9092),
        ]))

    @patch('afkak.client._get_IP_addresses')
    def test__collect_hosts__with_spaces(self, IP_addresses):
        hosts = "localhost:1234, localhost"
        IP_addresses.return_value = ['localhost']
        result = self.successResultOf(_collect_hosts(hosts))
        self.assertEqual(set(result), set([
            ('localhost', 1234),
            ('localhost', 9092),
        ]))

    @patch('afkak.client.DNSclient.lookupAddress')
    def test__get_IP_addresses(self, RRHeader):
        mock = MagicMock()
        ip_address = '127.0.0.1'
        mock.payload.dottedQuad.return_value = ip_address
        RRHeader.return_value = ([mock], [], [])
        result = self.successResultOf(_get_IP_addresses('address'))
        self.assertEqual(result, [ip_address])

        # Test given a host_address which is an IP address
        result = self.successResultOf(_get_IP_addresses(ip_address))
        self.assertEqual(result, [ip_address])

        # Test given a bad host_address
        RRHeader.return_value = ([], [], [])
        result = self.successResultOf(_get_IP_addresses(' '))
        self.assertEqual(None, result)

    @patch('afkak.client.DNSclient.lookupAddress')
    def test__get_IP_addresses_CNAME(self, RRHeader):
        cname = MagicMock(type=dns.A, spec=None)
        a = MagicMock()
        ip_address = '127.0.0.1'
        mock.payload.dottedQuad.return_value = ip_address
        RRHeader.return_value = ([cname, a], [], [])
        result = self.successResultOf(_get_IP_addresses('address'))
        self.assertEqual(result, [ip_address])

        # Test given a host_address which is an IP address
        result = self.successResultOf(_get_IP_addresses(ip_address))
        self.assertEqual(result, [ip_address])

        # Test given a bad host_address
        RRHeader.return_value = ([], [], [])
        result = self.successResultOf(_get_IP_addresses(' '))
        self.assertEqual(None, result)

    @patch('afkak.client.KafkaBrokerClient')
    def test_get_brokerclient(self, broker):
        """
        test_get_brokerclient
        """
        brokermocks = [MagicMock(), MagicMock(), MagicMock()]
        broker.side_effect = brokermocks
        broker_1 = MagicMock()
        broker_1.return_value = 'broker_1'
        broker_2 = MagicMock()
        broker_2.return_value = 'broker_2'
        broker_3 = MagicMock()
        broker_3.return_value = 'broker_3'

        client = KafkaClient(
            hosts=['broker_1:4567', 'broker_2', 'broker_3:45678'],
            timeout=None)
        client.clients = {('broker_1', 4567): broker_1,
                          ('broker_2', 9092): broker_2,
                          ('broker_3', 45678): broker_3}
        client._collect_hosts_d = None

        # Get the same host/port again, and make sure the same one is returned
        brkr = client._get_brokerclient('broker_2', DefaultKafkaPort)
        brkr()
        # Get another one...
        client._get_brokerclient('broker_3', 45678)
        # Get the first one again, and make sure the same one is returned
        brkr2 = client._get_brokerclient('broker_2', DefaultKafkaPort)
        brkr2()
        self.assertEqual(brkr, brkr2)
        # Get the last one
        client._get_brokerclient('broker_1', 4567)
        # Assure we got broker_2 twice
        self.assertEqual(len(broker_2.call_args_list), 2)

    @patch('afkak.client._collect_hosts')
    def test_update_broker_state(self, collected_hosts):
        """
        test_update_broker_state
        Make sure that the client logs when a broker changes state
        """
        client = KafkaClient(hosts=['broker_1:4567', 'broker_2',
                                    'broker_3:45678'])
        collected_hosts.return_value = [('broker_1', 4567),
                                        ('broker_2', 9092),
                                        ('broker_3', 45678)]
        e = ConnectionRefusedError()
        bkr = "aBroker"
        client.reset_all_metadata = MagicMock()
        client.load_metadata_for_topics = MagicMock()
        client._update_broker_state(bkr, False, e)
        client.reset_all_metadata.assert_called_once_with()
        client.load_metadata_for_topics.assert_called_once_with()

    @patch('afkak.client.KafkaBrokerClient')
    def test_update_brokers(self, broker):
        """
        test_update_brokers
        Test that we create/close brokers as they come and go in the metadata
        """
        brokermocks = [MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                       MagicMock(), MagicMock()]
        broker.side_effect = brokermocks
        client = KafkaClient(
            hosts=['broker_1:4567', 'broker_2', 'broker_3:45678'])
        client.clients = {('broker_1', 4567): MagicMock(),
                          ('broker_2', 9092): MagicMock(),
                          ('broker_3', 45678): MagicMock()}

        beforeClients = client.clients.copy()
        # Replace brokers with 'brokersX.afkak.example.com:100X' from way above
        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=lambda a, b: succeed(self.testMetaData)):
            client.load_metadata_for_topics()

        # Make sure the old brokers got 'close()'ed
        for brkr in beforeClients.values():
            brkr.close.assert_called_once_with()

        # Now remove one of the current clients
        new_clients = client.clients.keys()
        removed = client.clients[new_clients.pop()]

        client._update_brokers(new_clients, remove=True)
        # Removed should have been 'close()'d
        removed.close.assert_called_once_with()

        # close the client
        beforeClients = client.clients.copy()
        client.close()
        # and make sure the last brokers are closed
        for brkr in beforeClients.values():
            brkr.close.assert_called_once_with()

    def test_send_broker_aware_request(self):
        """
        test_send_broker_aware_request
        Test that send_broker_aware_request returns the proper responses
        when given the correct data
        """
        T1 = "Topic1"
        T2 = "Topic2"

        client = KafkaClient(hosts='kafka01:9092,kafka02:9092')

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host='kafka01', port=9092),
            BrokerMetadata(node_id=2, host='kafka02', port=9092),
            ]
        topic_partitions = {
            T1: [0],
            T2: [0],
            }
        client.topic_partitions = copy(topic_partitions)
        topics_to_brokers = {
            TopicAndPartition(topic=T1, partition=0): brokers[0],
            TopicAndPartition(topic=T2, partition=0): brokers[1],
            }
        client.topics_to_brokers = copy(topics_to_brokers)

        # Setup the payloads, encoder & decoder funcs
        payloads = [
            ProduceRequest(
                T1, 0, [create_message(T1 + " message %d" % i)
                        for i in range(10)]),
            ProduceRequest(
                T2, 0, [create_message(T2 + " message %d" % i)
                        for i in range(5)]),
            ]

        encoder = partial(
            KafkaCodec.encode_produce_request,
            acks=1, timeout=1000)
        decoder = KafkaCodec.decode_produce_response

        # patch the KafkaBrokerClient so it doesn't really connect
        with patch.object(KafkaBrokerClient, '_connect'):
            respD = client._send_broker_aware_request(
                payloads, encoder, decoder)
        # Shouldn't have a result yet. If we do, there was an error
        self.assertNoResult(respD)

        # Dummy up some responses, one for each broker.
        resp0 = struct.pack('>ih%dsiihq' % (len(T1)),
                            1, len(T1), T1, 1, 0, 0, 10L)
        resp1 = struct.pack('>ih%dsiihq' % (len(T2)),
                            1, len(T2), T2, 1, 0, 0, 20L)

        # "send" the results
        for topic, resp in ((T1, resp0), (T2, resp1)):
            brkr, reqs = brkrAndReqsForTopicAndPartition(client, topic)
            for req in reqs.values():
                brkr.handleResponse(struct.pack('>i', req.id) + resp)

        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(results,
                         [ProduceResponse(T1, 0, 0, 10L),
                          ProduceResponse(T2, 0, 0, 20L)])

        # Now try again, but with one request failing...
        with patch.object(KafkaBrokerClient, '_connect'):
            respD = client._send_broker_aware_request(
                payloads, encoder, decoder)

        # dummy responses
        resp0 = struct.pack('>ih%dsiihq' % (len(T1)),
                            1, len(T1), T1, 1, 0, 0, 10L)
        resp1 = struct.pack('>ih%dsiihq' % (len(T2)),
                            1, len(T2), T2, 1, 0, 7, 20L)
        # 'send' the response for T1 request
        brkr, reqs = brkrAndReqsForTopicAndPartition(client, T1)
        for req in reqs.values():
            brkr.handleResponse(struct.pack('>i', req.id) + resp0)

        # cancel the request for T2 request (timeout eailure)
        brkr, reqs = brkrAndReqsForTopicAndPartition(client, T2)
        for req in reqs.values():
            brkr.cancelRequest(req.id)

        # check the result. Should be Failure(FailedPayloadsError)
        results = self.failureResultOf(respD, FailedPayloadsError)
        # And the exceptions args should hold the payload of the
        # failed request
        self.assertEqual(results.value.args[1][0][0], payloads[1])

        # above failure reset the client's metadata, so we need to re-install
        client.topic_partitions = copy(topic_partitions)
        client.topics_to_brokers = copy(topics_to_brokers)

        # And finally, without expecting a response...
        for brkr in client.clients.values():
            brkr.proto = MagicMock()
        respD = client._send_broker_aware_request(payloads, encoder, None)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(results, [])

    def test_reset_topic_metadata(self):
        """ test_reset_topic_metadata
        Test that reset_topic_metadata makes the proper changes
        to the client's metadata
        """
        client = KafkaClient(hosts='kafka01:9092,kafka02:9092')

        # Setup the client with the metadata we want start with
        Ts = ["Topic1", "Topic2", "Topic3", "Topic4"]
        brokers = [
            BrokerMetadata(node_id=1, host='kafka01', port=9092),
            BrokerMetadata(node_id=2, host='kafka02', port=9092),
            ]
        client.topic_partitions = {
            Ts[0]: [0],
            Ts[1]: [0],
            Ts[2]: [0, 1, 2, 3],
            Ts[3]: [],
            }
        client.topic_errors = {
            Ts[0]: 0,
            Ts[1]: 0,
            Ts[2]: 0,
            Ts[3]: 5,
            }
        client.topics_to_brokers = {
            TopicAndPartition(topic=Ts[0], partition=0): brokers[0],
            TopicAndPartition(topic=Ts[1], partition=0): brokers[1],
            TopicAndPartition(topic=Ts[2], partition=0): brokers[0],
            TopicAndPartition(topic=Ts[2], partition=1): brokers[1],
            TopicAndPartition(topic=Ts[2], partition=2): brokers[0],
            TopicAndPartition(topic=Ts[2], partition=3): brokers[1],
            }

        # make copies...
        tParts = copy(client.topic_partitions)
        topicsToBrokers = copy(client.topics_to_brokers)

        # Check if we can get the error code. This should maybe have a separate
        # test, but the setup is a pain, and it's all done here...
        self.assertEqual(client.metadata_error_for_topic(Ts[3]), 5)
        client.reset_topic_metadata(Ts[3])
        self.assertEqual(client.metadata_error_for_topic(Ts[3]),
                         UnknownTopicOrPartitionError.errno)

        # Reset the client's metadata for Topic2
        client.reset_topic_metadata(Ts[1])

        # No change to brokers...
        # topics_to_brokers gets every (topic,partition) tuple removed
        # for that topic
        del topicsToBrokers[TopicAndPartition(topic=Ts[1], partition=0)]
        # topic_paritions gets topic deleted
        del tParts[Ts[1]]
        del tParts[Ts[3]]
        # Check correspondence
        self.assertEqual(topicsToBrokers, client.topics_to_brokers)
        self.assertEqual(tParts, client.topic_partitions)

        # Resetting an unknown topic has no effect
        client.reset_topic_metadata("bogus")
        # Check correspondence with unchanged copies
        self.assertEqual(topicsToBrokers, client.topics_to_brokers)
        self.assertEqual(tParts, client.topic_partitions)

    def test_has_metadata_for_topic(self):
        """ test_has_metatdata_for_topic
        """
        client = KafkaClient(hosts='kafka01:9092,kafka02:9092')

        # Setup the client with the metadata we want start with
        Ts = ["Topic1", "Topic2", "Topic3"]
        brokers = [
            BrokerMetadata(node_id=1, host='kafka01', port=9092),
            BrokerMetadata(node_id=2, host='kafka02', port=9092),
            ]
        client.topic_partitions = {
            Ts[0]: [0],
            Ts[1]: [0],
            Ts[2]: [0, 1, 2, 3],
            }
        client.topics_to_brokers = {
            TopicAndPartition(topic=Ts[0], partition=0): brokers[0],
            TopicAndPartition(topic=Ts[1], partition=0): brokers[1],
            TopicAndPartition(topic=Ts[2], partition=0): brokers[0],
            TopicAndPartition(topic=Ts[2], partition=1): brokers[1],
            TopicAndPartition(topic=Ts[2], partition=2): brokers[0],
            TopicAndPartition(topic=Ts[2], partition=3): brokers[1],
            }

        for topic in Ts:
            self.assertTrue(client.has_metadata_for_topic(topic))
        self.assertFalse(client.has_metadata_for_topic("Unknown"))

    def test_reset_all_metadata(self):
        """ test_reset_all_metadata
        """
        client = KafkaClient(hosts='kafka01:9092,kafka02:9092')

        # Setup the client with the metadata we want start with
        Ts = ["Topic1", "Topic2", "Topic3"]
        brokers = [
            BrokerMetadata(node_id=1, host='kafka01', port=9092),
            BrokerMetadata(node_id=2, host='kafka02', port=9092),
            ]
        client.topic_partitions = {
            Ts[0]: [0],
            Ts[1]: [0],
            Ts[2]: [0, 1, 2, 3],
            }
        client.topics_to_brokers = {
            TopicAndPartition(topic=Ts[0], partition=0): brokers[0],
            TopicAndPartition(topic=Ts[1], partition=0): brokers[1],
            TopicAndPartition(topic=Ts[2], partition=0): brokers[0],
            TopicAndPartition(topic=Ts[2], partition=1): brokers[1],
            TopicAndPartition(topic=Ts[2], partition=2): brokers[0],
            TopicAndPartition(topic=Ts[2], partition=3): brokers[1],
            }

        for topic in Ts:
            self.assertTrue(client.has_metadata_for_topic(topic))
        self.assertFalse(client.has_metadata_for_topic("Unknown"))

        client.reset_all_metadata()
        self.assertEqual(client.topics_to_brokers, {})
        self.assertEqual(client.topic_partitions, {})

    def test_client_close(self):
        mocked_brokers = {
            ('kafka91', 9092): MagicMock(),
            ('kafka92', 9092): MagicMock()
        }

        client = KafkaClient(hosts='kafka91:9092,kafka92:9092')

        # patch in our fake brokers
        client.clients = mocked_brokers
        client.close()

        # Check that each fake broker had its close() called
        for broker in mocked_brokers.values():
            broker.close.assert_called_once_with()

    def test_client_close_no_clients(self):
        client = KafkaClient(hosts=[])
        client.close()

    @patch('afkak.client._collect_hosts')
    def test_client_close_during_metadata_load(self, collected_hosts):
        collected_hosts.return_value = [('kafka', 9092)]
        mockbroker = Mock()
        d = Deferred()
        mockbroker.makeRequest.return_value = d
        mocked_brokers = {
            ('kafka', 9092): mockbroker,
        }

        client = KafkaClient(hosts='kafka:9092')

        # patch in our fake brokers
        client.clients = mocked_brokers
        client.load_metadata_for_topics()
        load_d = client.load_metadata
        mockbroker.makeRequest.assert_called_once_with(1, ANY)
        client.close()

        # Cancel the request as a real brokerclient would
        d.cancel()
        # Check that the load_metadata request was cancelled
        self.assertTrue(load_d.called)

        # Check that each fake broker had its close() called
        for broker in mocked_brokers.values():
            broker.close.assert_called_once_with()

    def test_load_consumer_metadata_for_group(self):
        """test_load_consumer_metadata_for_group

        Test that a subsequent load request for the same group before the
        the previous one has completed does not make a new request, and that
        a request for a different group does make a new request. Also, check
        That once the request completes for 'Group1', that subsequent requests
        will make a new request.
        """
        G1 = "ConsumerGroup1"
        G2 = "ConsumerGroup2"
        response = "".join([
            struct.pack('>i', 4),           # Correlation ID
            struct.pack('>h', 0),           # Error Code
            struct.pack('>i', 0),           # Coordinator id
            struct.pack('>h5s', 5, "host1"),  # The Coordinator host
            struct.pack('>i', 9092),          # The Coordinator port
        ])
        request_ds = [Deferred(), Deferred(), Deferred()]

        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=request_ds):
            client = KafkaClient(hosts='host1:9092')
            load1_d = client.load_consumer_metadata_for_group(G1)
            load2_d = client.load_consumer_metadata_for_group(G2)
            load3_d = client.load_consumer_metadata_for_group(G1)
            # Request 1 & 3 should return the same deferred.
            # Request 2 should be distinct
            self.assertEqual(request_ds[0], load1_d)
            self.assertEqual(load1_d, load3_d)
            self.assertEqual(request_ds[1], load2_d)
            # Now 'send' a response to the first/3rd requests
            request_ds[0].callback(response)
            # After response, new request for same group gets new deferred
            load4_d = client.load_consumer_metadata_for_group(G1)
            self.assertNotEqual(load1_d, load4_d)
            self.assertEqual(request_ds[2], load4_d)

            # Clean up outstanding requests by 'sending same response'
            request_ds[1].callback(response)
            request_ds[2].callback(response)
            for req_d in request_ds:
                self.assertTrue(self.successResultOf(req_d))
            client.close()

    def test_load_consumer_metadata_for_group_failure(self):
        """test_load_consumer_metadata_for_group_failure

        Test that a failure to retrieve the metadata for a group properly
        raises a ConsumerCoordinatorNotAvailableError exception
        """
        G1 = "ConsumerGroup1"
        G2 = "ConsumerGroup2"
        response = "".join([
            struct.pack('>i', 6),           # Correlation ID
            struct.pack('>h', 15),          # Error Code
            struct.pack('>i', -1),          # Coordinator id
            struct.pack('>h0s', 0, ""),     # The Coordinator host
            struct.pack('>i', -1),          # The Coordinator port
        ])

        d1 = Deferred()
        d2 = Deferred()
        request_ds = [d1, d2]

        with patch.object(KafkaClient, '_send_broker_unaware_request',
                          side_effect=request_ds):
            client = KafkaClient(hosts='host1:9092')
            load1_d = client.load_consumer_metadata_for_group(G1)
            load2_d = client.load_consumer_metadata_for_group(G2)
            # Check we got the right ones back
            self.assertEqual(request_ds[0], load1_d)
            self.assertEqual(request_ds[1], load2_d)

            # Now 'send' an error response via errBack() to the first request
            request_ds[0].errback(KafkaUnavailableError('No Kafka Available'))
            # And callback the 2nd with a response with an error code
            request_ds[1].callback(response)
            for req_d in request_ds:
                self.assertTrue(self.failureResultOf(
                    req_d, ConsumerCoordinatorNotAvailableError))
            client.close()

    def test_send_produce_request(self):
        """test_send_produce_request
        Test send_produce_request
        """
        T1 = "Topic1"
        T2 = "Topic2"
        mocked_brokers = {
            ('kafka31', 9092): MagicMock(),
            ('kafka32', 9092): MagicMock(),
        }
        # inject broker side effects
        ds = [[Deferred(), Deferred(), Deferred(), Deferred(), ],
              [Deferred(), Deferred(), Deferred(), Deferred(), ],
              ]
        mocked_brokers[('kafka31', 9092)].makeRequest.side_effect = ds[0]
        mocked_brokers[('kafka32', 9092)].makeRequest.side_effect = ds[1]

        def mock_get_brkr(host, port):
            return mocked_brokers[(host, port)]

        client = KafkaClient(hosts='kafka31:9092,kafka32:9092')

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host='kafka31', port=9092),
            BrokerMetadata(node_id=2, host='kafka32', port=9092),
            ]
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
                T1, 0, [create_message(T1 + " message %d" % i
                                       ) for i in range(10)]
                ),
            ProduceRequest(
                T2, 0, [create_message(T2 + " message %d" % i
                                       ) for i in range(5)]
                ),
            ]

        # patch the client so we control the brokerclients
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_produce_request(payloads)

        # Dummy up some responses, one from each broker
        corlID = 9876
        resp0 = struct.pack('>iih%dsiihq' % (len(T1)),
                            corlID, 1, len(T1), T1, 1, 0, 0, 10L)
        resp1 = struct.pack('>iih%dsiihq' % (len(T2)),
                            corlID + 1, 1, len(T2), T2, 1, 0, 0, 20L)
        # 'send' the responses
        ds[0][0].callback(resp0)
        ds[1][0].callback(resp1)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(results,
                         [ProduceResponse(T1, 0, 0, 10L),
                          ProduceResponse(T2, 0, 0, 20L)])

        # And again, with acks=0
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_produce_request(payloads, acks=0)
        ds[0][1].callback(None)
        ds[1][1].callback(None)
        results = list(self.successResultOf(respD))
        self.assertEqual(results, [])

        # And again, this time with an error coming back...
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_produce_request(payloads)

        # Dummy up some responses, one from each broker
        corlID = 13579
        resp0 = struct.pack('>iih%dsiihq' % (len(T1)),
                            corlID, 1, len(T1), T1, 1, 0, 0, 10L)
        resp1 = struct.pack('>iih%dsiihq' % (len(T2)),
                            corlID + 1, 1, len(T2), T2, 1, 0,
                            6, 20L)  # NotLeaderForPartition=6
        with patch.object(KafkaClient, 'reset_topic_metadata') as rtmdMock:
            # The error we return here should cause a metadata reset for the
            # erroring topic
            ds[0][2].callback(resp0)
            ds[1][2].callback(resp1)
            rtmdMock.assert_called_once_with(T2)
        # check the results
        self.successResultOf(
            self.failUnlessFailure(respD, NotLeaderForPartitionError))

        # And again, this time with an error coming back...but ignored,
        # and a callback to pre-process the response
        def preprocCB(response):
            return response
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_produce_request(payloads, fail_on_error=False,
                                                callback=preprocCB)

        # Dummy up some responses, one from each broker
        corlID = 13579
        resp0 = struct.pack('>iih%dsiihq' % (len(T1)),
                            corlID, 1, len(T1), T1, 1, 0, 0, 10L)
        resp1 = struct.pack('>iih%dsiihq' % (len(T2)),
                            corlID + 1, 1, len(T2), T2, 1, 0,
                            6, 20L)  # NotLeaderForPartition=6
        # 'send' the responses
        ds[0][3].callback(resp0)
        ds[1][3].callback(resp1)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(results,
                         [ProduceResponse(T1, 0, 0, 10L),
                          ProduceResponse(T2, 0, 6, 20L)])

    def test_send_fetch_request(self):
        """test_send_fetch_request
        Test send_fetch_request
        """
        T1 = "Topic41"
        T2 = "Topic42"
        mocked_brokers = {
            ('kafka41', 9092): MagicMock(),
            ('kafka42', 9092): MagicMock(),
        }
        # inject broker side effects
        ds = [[Deferred(), Deferred(), Deferred(), Deferred(), ],
              [Deferred(), Deferred(), Deferred(), Deferred(), ],
              ]
        mocked_brokers[('kafka41', 9092)].makeRequest.side_effect = ds[0]
        mocked_brokers[('kafka42', 9092)].makeRequest.side_effect = ds[1]

        def mock_get_brkr(host, port):
            return mocked_brokers[(host, port)]

        client = KafkaClient(hosts='kafka41:9092,kafka42:9092')

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host='kafka41', port=9092),
            BrokerMetadata(node_id=2, host='kafka42', port=9092),
            ]
        client.topic_partitions = {
            T1: [0],
            T2: [0],
            }
        client.topics_to_brokers = {
            TopicAndPartition(topic=T1, partition=0): brokers[0],
            TopicAndPartition(topic=T2, partition=0): brokers[1],
            }

        # Setup the payloads
        payloads = [FetchRequest(T1, 0, 0, 1024),
                    FetchRequest(T2, 0, 0, 1024),
                    ]

        # patch the client so we control the brokerclients
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_fetch_request(payloads)

        # Dummy up some responses, the same from both brokers for simplicity
        msgs = map(create_message, ["message1", "hi", "boo", "foo", "so fun!"])
        ms1 = KafkaCodec._encode_message_set([msgs[0], msgs[1]], offset=0)
        ms2 = KafkaCodec._encode_message_set([msgs[2]], offset=0)
        ms3 = KafkaCodec._encode_message_set([msgs[3], msgs[4]], offset=48)

        # Note this includes a response for a topic/partition we didn't request
        # and further, that response has an error. However,
        # '_send_broker_aware_request' won't return it to us since it doesn't
        # match our request, so we don't error out.
        encoded = struct.pack('>iih%dsiihqi%dsihqi%dsh%dsiihqi%ds' %
                              (len(T1), len(ms1), len(ms2), len(T2), len(ms3)),
                              2345, 2,  # Correlation ID, Num Topics
                              len(T1), T1, 2, 0, 0, 10, len(ms1), ms1, 1,
                              1, 20, len(ms2), ms2,  # Topic41, 2 partitions,
                              # part0, no-err, high-water-mark-offset, msg1
                              # part1, err=1, high-water-mark-offset, msg1
                              len(T2), T2, 1, 0, 0, 30, len(ms3), ms3)
        # 'send' the responses
        ds[0][0].callback(encoded)
        ds[1][0].callback(encoded)
        # check the results
        results = list(self.successResultOf(respD))

        def expand_messages(response):
            return FetchResponse(response.topic, response.partition,
                                 response.error, response.highwaterMark,
                                 list(response.messages))
        expanded_responses = map(expand_messages, results)
        expect = [FetchResponse(T1, 0, 0, 10, [OffsetAndMessage(0, msgs[0]),
                                               OffsetAndMessage(1, msgs[1])]),
                  FetchResponse(T2, 0, 0, 30, [OffsetAndMessage(48, msgs[3]),
                                               OffsetAndMessage(49, msgs[4])])]
        self.assertEqual(expect, expanded_responses)

        # Again, with a callback
        def preprocCB(response):
            return response
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_fetch_request(payloads, callback=preprocCB)
        # 'send' the responses
        ds[0][1].callback(encoded)
        ds[1][1].callback(encoded)
        # check the results
        results = list(self.successResultOf(respD))
        expanded_responses = map(expand_messages, results)
        expect = [FetchResponse(T1, 0, 0, 10, [OffsetAndMessage(0, msgs[0]),
                                               OffsetAndMessage(1, msgs[1])]),
                  FetchResponse(T2, 0, 0, 30, [OffsetAndMessage(48, msgs[3]),
                                               OffsetAndMessage(49, msgs[4])])]
        self.assertEqual(expect, expanded_responses)

    def test_send_fetch_request_bad_timeout(self):
        client = KafkaClient(hosts='kafka41:9092,kafka42:9092')
        payload = [FetchRequest('T1', 0, 0, 1024)]
        timeout = client.timeout * 1000  # client.timeout=secs, max_wait=msecs
        d = client.send_fetch_request(payload, max_wait_time=timeout)
        self.failureResultOf(d, ValueError)

    def test_send_offset_request(self):
        """test_send_offset_request
        Test send_offset_request
        """
        T1 = "Topic51"
        T2 = "Topic52"
        mocked_brokers = {
            ('kafka51', 9092): MagicMock(),
            ('kafka52', 9092): MagicMock(),
        }
        # inject broker side effects
        ds = [[Deferred(), Deferred(), Deferred(), Deferred(), ],
              [Deferred(), Deferred(), Deferred(), Deferred(), ],
              ]
        mocked_brokers[('kafka51', 9092)].makeRequest.side_effect = ds[0]
        mocked_brokers[('kafka52', 9092)].makeRequest.side_effect = ds[1]

        def mock_get_brkr(host, port):
            return mocked_brokers[(host, port)]

        client = KafkaClient(hosts='kafka51:9092,kafka52:9092')

        # Setup the client with the metadata we want it to have
        brokers = [
            BrokerMetadata(node_id=1, host='kafka51', port=9092),
            BrokerMetadata(node_id=2, host='kafka52', port=9092),
            ]
        client.topic_partitions = {
            T1: [0],
            T2: [0],
            }
        client.topics_to_brokers = {
            TopicAndPartition(topic=T1, partition=0): brokers[0],
            TopicAndPartition(topic=T2, partition=0): brokers[1],
            }

        # Setup the payloads
        payloads = [OffsetRequest(T1, 0, -1, 20),  # ask for up to 20
                    OffsetRequest(T2, 0, -2, 1),  # -2==earliest, only get 1
                    ]

        # patch the client so we control the brokerclients
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_offset_request(payloads)

        # Dummy up some responses, one from each broker
        resp1 = "".join([
            struct.pack(">i", 42),            # Correlation ID
            struct.pack(">i", 1),             # One topics
            struct.pack(">h7s", 7, T1),       # First topic
            struct.pack(">i", 1),             # 1 partition

            struct.pack(">i", 0),             # Partition 0
            struct.pack(">h", 0),             # No error
            struct.pack(">i", 3),             # 3 offsets
            struct.pack(">q", 96),            # Offset 96
            struct.pack(">q", 98),            # Offset 98
            struct.pack(">q", 99),            # Offset 99
        ])
        resp2 = "".join([
            struct.pack(">i", 68),            # Correlation ID
            struct.pack(">i", 1),             # One topic
            struct.pack(">h7s", 7, T2),       # First topic
            struct.pack(">i", 1),             # 1 partition

            struct.pack(">i", 0),             # Partition 0
            struct.pack(">h", 0),             # No error
            struct.pack(">i", 1),             # 1 offset
            struct.pack(">q", 4096),          # Offset 4096
        ])

        # 'send' the responses
        ds[0][0].callback(resp1)
        ds[1][0].callback(resp2)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(set(results), set([
            OffsetResponse(topic=T1, partition=0, error=0,
                           offsets=(96, 98, 99,)),
            OffsetResponse(topic=T2, partition=0, error=0,
                           offsets=(4096,)),
        ]))

        # Again, with a callback
        def preprocCB(response):
            return response
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_offset_request(payloads, callback=preprocCB)

        # 'send' the responses
        ds[0][1].callback(resp1)
        ds[1][1].callback(resp2)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(set(results), set([
            OffsetResponse(topic=T1, partition=0, error=0,
                           offsets=(96, 98, 99,)),
            OffsetResponse(topic=T2, partition=0, error=0,
                           offsets=(4096,)),
        ]))

    def test_send_offset_fetch_request(self):
        """test_send_offset_fetch_request"""
        T1 = "Topic71"
        T2 = "Topic72"
        G1 = "ConsumerGroup1"
        G2 = "ConsumerGroup2"
        mock_load_cmfg_calls = {G1: 0, G2: 0}
        mocked_brokers = {
            ('kafka71', 9092): MagicMock(),
            ('kafka72', 9092): MagicMock(),
        }

        # inject broker side effects
        ds = [[Deferred(), Deferred(), Deferred(), Deferred(), ],
              [Deferred(), Deferred(), Deferred(), Deferred(), ],
              ]

        mocked_brokers[('kafka71', 9092)].makeRequest.side_effect = ds[0]
        mocked_brokers[('kafka72', 9092)].makeRequest.side_effect = ds[1]

        brokers = [
            BrokerMetadata(node_id=1, host='kafka71', port=9092),
            BrokerMetadata(node_id=2, host='kafka72', port=9092),
            ]
        broker_for_group = {
            G1: brokers[0],
            G2: brokers[1],
            }

        def mock_get_brkr(host, port):
            return mocked_brokers[(host, port)]

        def mock_load_cmfg(group):
            mock_load_cmfg_calls[group] += 1
            client.consumer_group_to_brokers[group] = broker_for_group[group]
            return True

        client = KafkaClient(hosts='kafka71:9092,kafka72:9092')
        client.load_consumer_metadata_for_group = mock_load_cmfg

        # Setup the payloads
        payloads = [OffsetFetchRequest(T1, 71),
                    OffsetFetchRequest(T2, 72),
                    ]

        # Dummy the response
        resp = "".join([
            struct.pack(">i", 42),            # Correlation ID
            struct.pack(">i", 2),             # Two topics
            struct.pack(">h7s", 7, T1),       # First topic
            struct.pack(">i", 1),             # 1 partition
            struct.pack(">i", 71),            # Partition 71
            struct.pack(">q", 49),            # Offset 49
            struct.pack(">h9s", 9, "Metadata1"),  # Metadata
            struct.pack(">h", 0),             # No error
            struct.pack(">h7s", 7, T2),       # Second topic
            struct.pack(">i", 1),             # 1 partition
            struct.pack(">i", 72),            # Partition 72
            struct.pack(">q", 27),            # Offset 27
            struct.pack(">h9s", 9, "Metadata2"),  # Metadata
            struct.pack(">h", 0),             # No error
        ])

        # patch the client so we control the brokerclients
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_offset_fetch_request(G1, payloads)

        # That first lookup for the group should result in one call to
        # load_consumer_metadata_for_group
        self.assertEqual(mock_load_cmfg_calls[G1], 1)
        # 'send' the response
        ds[0][0].callback(resp)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(set(results), set([
            OffsetFetchResponse(topic=T1, partition=71, offset=49,
                                metadata='Metadata1', error=0),
            OffsetFetchResponse(topic=T2, partition=72, offset=27,
                                metadata='Metadata2', error=0),
        ]))

        # Again, with a callback (for full coverage)
        preproc_call_count = [0]

        def preprocCB(response):
            preproc_call_count[0] += 1
            return response

        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_offset_fetch_request(
                G1, payloads, callback=preprocCB)

        # Check that another call is not made for the same group
        self.assertEqual(mock_load_cmfg_calls[G1], 1)
        # 'send' the responses
        ds[0][1].callback(resp)
        # check the results
        results = list(self.successResultOf(respD))
        # Test that the preprocessor callback was called (2 responses)
        self.assertEqual(preproc_call_count[0], 2)

        self.assertEqual(set(results), set([
            OffsetFetchResponse(topic=T1, partition=71, offset=49,
                                metadata='Metadata1', error=0),
            OffsetFetchResponse(topic=T2, partition=72, offset=27,
                                metadata='Metadata2', error=0),
        ]))
        client.close()

    def test_send_offset_fetch_request_failure(self):
        """test_send_offset_fetch_request_failure

        Test that when a request involving a consumer metadata broker fails
        that we reset the cached broker for the consumer group.
        """
        T1 = "Topic71"
        G1 = "ConsumerGroup1"
        G2 = "ConsumerGroup2"
        mock_load_cmfg_calls = {G1: 0, G2: 0}
        mocked_brokers = {
            ('kafka71', 9092): MagicMock(),
            ('kafka72', 9092): MagicMock(),
        }

        # inject broker side effects
        ds = [[Deferred(), Deferred(), Deferred(), Deferred(), ],
              [Deferred(), Deferred(), Deferred(), Deferred(), ],
              ]

        mocked_brokers[('kafka71', 9092)].makeRequest.side_effect = ds[0]
        mocked_brokers[('kafka72', 9092)].makeRequest.side_effect = ds[1]

        brokers = [
            BrokerMetadata(node_id=1, host='kafka71', port=9092),
            BrokerMetadata(node_id=2, host='kafka72', port=9092),
            ]
        broker_for_group = {
            G1: brokers[0],
            G2: brokers[1],
            }

        def mock_get_brkr(host, port):
            return mocked_brokers[(host, port)]

        def mock_load_cmfg(group):
            mock_load_cmfg_calls[group] += 1
            client.consumer_group_to_brokers[group] = broker_for_group[group]
            return True

        client = KafkaClient(hosts='kafka71:9092,kafka72:9092')
        client.load_consumer_metadata_for_group = mock_load_cmfg

        # Setup the payload
        payloads = [OffsetFetchRequest(T1, 78)]

        # Dummy the response
        resp = "".join([
            struct.pack(">i", 42),  # Correlation ID
            struct.pack(">i", 1),   # 1 topic
            struct.pack(">h7s", 7, T1),  # Topic
            struct.pack(">i", 1),   # 1 partition
            struct.pack(">i", 78),  # Partition 78
            struct.pack(">q", -1),  # Offset -1
            struct.pack(">h", 0),   # Metadata
            struct.pack(">h", 16),  # NotCoordinatorForConsumerError
        ])

        # patch the client so we control the brokerclients
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_offset_fetch_request(G1, payloads)

        # That first lookup for the group should result in one call to
        # load_consumer_metadata_for_group
        self.assertEqual(mock_load_cmfg_calls[G1], 1)
        # And the cache of the broker for the consumer group should be set
        self.assertEqual(client.consumer_group_to_brokers[G1],
                         broker_for_group[G1])
        # 'send' the response
        ds[0][0].callback(resp)
        # check the results
        self.assertTrue(
            self.failureResultOf(respD, NotCoordinatorForConsumerError))
        self.assertNotIn(G1, client.consumer_group_to_brokers)
        # cleanup
        client.close()

    def test_send_offset_commit_request(self):
        """test_send_offset_commit_request"""
        T1 = "Topic61"
        T2 = "Topic62"
        G1 = "ConsumerGroup1"
        G2 = "ConsumerGroup2"
        mock_load_cmfg_calls = {G1: 0, G2: 0}

        mocked_brokers = {
            ('kafka61', 9092): MagicMock(),
            ('kafka62', 9092): MagicMock(),
        }
        # inject broker side effects
        ds = [[Deferred(), Deferred(), Deferred(), Deferred(), ],
              [Deferred(), Deferred(), Deferred(), Deferred(), ],
              ]

        mocked_brokers[('kafka61', 9092)].makeRequest.side_effect = ds[0]
        mocked_brokers[('kafka62', 9092)].makeRequest.side_effect = ds[1]

        brokers = [
            BrokerMetadata(node_id=1, host='kafka61', port=9092),
            BrokerMetadata(node_id=2, host='kafka62', port=9092),
            ]
        broker_for_group = {
            G1: brokers[0],
            G2: brokers[1],
            }

        def mock_get_brkr(host, port):
            return mocked_brokers[(host, port)]

        def mock_load_cmfg(group):
            mock_load_cmfg_calls[group] += 1
            client.consumer_group_to_brokers[group] = broker_for_group[group]
            return True

        client = KafkaClient(hosts='kafka61:9092,kafka62:9092')
        client.load_consumer_metadata_for_group = mock_load_cmfg

        # Setup the client with the metadata we want it to have
        client.topic_partitions = {
            T1: [61],
            T2: [62],
            }
        client.topics_to_brokers = {
            TopicAndPartition(topic=T1, partition=61): brokers[0],
            TopicAndPartition(topic=T2, partition=62): brokers[1],
            }

        # Setup the payloads
        payloads = [
            OffsetCommitRequest(T1, 61, 81, -1, "metadata1"),
            OffsetCommitRequest(T2, 62, 91, -1, "metadata2"),
            ]

        # patch the client so we control the brokerclients
        with patch.object(KafkaClient, '_get_brokerclient',
                          side_effect=mock_get_brkr):
            respD = client.send_offset_commit_request(G2, payloads)

        # Dummy up a response for the commit
        resp1 = "".join([
            struct.pack(">i", 42),            # Correlation ID
            struct.pack(">i", 2),             # Two topics
            struct.pack(">h7s", 7, T1),       # First topic
            struct.pack(">i", 1),             # 1 partition
            struct.pack(">i", 61),            # Partition 61
            struct.pack(">h", 0),             # No error
            struct.pack(">h7s", 7, T2),       # Second topic
            struct.pack(">i", 1),             # 1 partition
            struct.pack(">i", 62),            # Partition 62
            struct.pack(">h", 0),             # No error
        ])

        # 'send' the responses
        ds[1][0].callback(resp1)
        # check the results
        results = list(self.successResultOf(respD))
        self.assertEqual(set(results), set([
            OffsetCommitResponse(topic=T1, partition=61, error=0),
            OffsetCommitResponse(topic=T2, partition=62, error=0),
        ]))

        client.close()

    def test_send_offset_commit_request_failure(self):
        """test_send_offset_commit_request_failure

        Test that when the kafka broker is unavailable, that the proper
        ConsumerCoordinatorNotAvailableError is raised"""

        T1 = "Topic61"
        G1 = "ConsumerGroup1"

        def mock_gcfg(group):
            return None

        client = KafkaClient(hosts='kafka61:9092')
        client._get_coordinator_for_group = mock_gcfg

        # Setup the client with the metadata we want it to have
        client.topic_partitions = {
            T1: [61],
            }

        # Setup the payloads
        payloads = [
            OffsetCommitRequest(T1, 61, 81, -1, "metadata1"),
            ]

        respD1 = client.send_offset_commit_request(G1, [])
        self.assertTrue(
            self.failureResultOf(respD1, ValueError))
        respD2 = client.send_offset_commit_request(G1, payloads)
        self.assertTrue(
            self.failureResultOf(respD2, ConsumerCoordinatorNotAvailableError))
        client.close()
