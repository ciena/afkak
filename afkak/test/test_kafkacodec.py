# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2017, 2018, 2019 Ciena Corporation.
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
Test the KafkaCodec class.
"""

from __future__ import absolute_import, division

import struct
from contextlib import contextmanager
from unittest import SkipTest, TestCase

import mock
import six
from mock import sentinel

import afkak.kafkacodec
from afkak.codec import gzip_decode, has_snappy, snappy_decode
from afkak.common import (
    BrokerMetadata, ChecksumError, ConsumerFetchSizeTooSmall,
    ConsumerMetadataResponse, FetchRequest, FetchResponse, InvalidMessageError,
    Message, OffsetAndMessage, OffsetCommitRequest, OffsetCommitResponse,
    OffsetFetchRequest, OffsetFetchResponse, OffsetRequest, OffsetResponse,
    PartitionMetadata, ProduceRequest, ProduceResponse, ProtocolError,
    TopicMetadata, UnsupportedCodecError,
)
from afkak.kafkacodec import (
    ATTRIBUTE_CODEC_MASK, CODEC_GZIP, CODEC_NONE, CODEC_SNAPPY, KafkaCodec,
    _ReprRequest, create_gzip_message, create_message, create_message_set,
    create_snappy_message,
)

from .testutil import make_send_requests


def create_encoded_metadata_response(broker_data, topic_data):
    encoded = struct.pack('>ii', 3, len(broker_data))
    for node_id, broker in broker_data.items():
        assert isinstance(broker.host, six.string_types), "{!r} must be string".format(broker.host)
        encoded += struct.pack('>ih', node_id, len(broker.host))
        encoded += broker.host.encode('ascii')
        encoded += struct.pack('>i', broker.port)

    encoded += struct.pack('>i', len(topic_data))
    for topic, topic_metadata in topic_data.items():
        assert isinstance(topic, six.string_types), "{!r} must be string".format(topic)
        _, topic_err, partitions = topic_metadata
        encoded += struct.pack('>hh',  topic_err, len(topic))
        encoded += topic.encode('ascii')
        encoded += struct.pack('>i', len(partitions))
        for partition, metadata in partitions.items():
            encoded += struct.pack('>hiii',
                                   metadata.partition_error_code,
                                   partition, metadata.leader,
                                   len(metadata.replicas))
            for replica in metadata.replicas:
                encoded += struct.pack('>i', replica)
            encoded += struct.pack('>i', len(metadata.isr))
            for isr in metadata.isr:
                encoded += struct.pack('>i', isr)

    return encoded


class ReprRequestTests(TestCase):
    def test_produce(self):
        rr = _ReprRequest((
            b'\x00\x00'
            b'\x00\x03'
            b'\x00\x00\x04\x00'
            b'xx'
        ))
        self.assertEqual('ProduceRequest3 correlationId=1024 (10 bytes)', str(rr))

    def test_unknown_key(self):
        rr = _ReprRequest((
            b'\x00\xff'
            b'\x00\x01'
            b'\x00\x00\x00\x00'
        ))
        self.assertEqual('request key=255v1 correlationId=0 (8 bytes)', str(rr))

    def test_too_short(self):
        rr = _ReprRequest(b'\x00\x00\xab\xcd')
        self.assertEqual("invalid request (0000abcd)", str(rr))


class TestKafkaCodec(TestCase):
    def test_create_message(self):
        payload = b"test"
        key = b"key"
        msg = create_message(payload, key)
        self.assertEqual(msg.magic, 1)
        self.assertEqual(msg.attributes, 0)
        self.assertEqual(msg.key, key)
        self.assertEqual(msg.value, payload)

    def test_create_gzip(self):
        message_list = [create_message((-1, b"v1"), None),
                        create_message((-1, b"v2"), key=b'42')]
        
        msg = create_gzip_message(message_list)
        self.assertEqual(msg.magic, 1)
        self.assertEqual(msg.attributes, ATTRIBUTE_CODEC_MASK & CODEC_GZIP)
        self.assertEqual(msg.key, None)
        # Need to decode to check since gzipped payload is non-deterministic
        decoded = gzip_decode(msg.value)
        expect = b"".join([
            struct.pack(">q", 0),           # MsgSet offset
            struct.pack(">i", 24),          # MsgSet size
            struct.pack(">i", -346518548),  # CRC
            struct.pack(">bbq", 1, 0, -1),       # Magic, flags, timestamp
            struct.pack(">i", -1),          # -1 indicates a null key
            struct.pack(">i", 2),           # Msg length (bytes)
            b"v1",                          # Message contents

            struct.pack(">q", 0),           # MsgSet offset
            struct.pack(">i", 26),          # MsgSet size
            struct.pack(">i", -19620068),  # CRC
            struct.pack(">bbq", 1, 0, -1),       # Magic, flags, timestamp
            struct.pack(">i2s", 2, b'42'),  # Key is 2 bytes long, 42
            struct.pack(">i", 2),           # Msg length (bytes)
            b"v2",                          # Message contents
        ])        
        self.assertEqual(decoded, expect)

    def test_create_snappy(self):
        if not has_snappy():
            raise SkipTest("Snappy not available")  # pragma: no cover
        message_list = [create_message((47, b"v3"), key=b'84'),
                        create_message((48, b"v4"), None)]

        msg = create_snappy_message(message_list)
        self.assertEqual(msg.magic, 1)
        self.assertEqual(msg.attributes, ATTRIBUTE_CODEC_MASK & CODEC_SNAPPY)
        self.assertEqual(msg.key, None)
        decoded = snappy_decode(msg.value)
        expect = b"".join([
            struct.pack(">q", 0),           # MsgSet offset
            struct.pack(">i", 26),          # MsgSet size
            struct.pack(">i", -892550681),   # CRC
            struct.pack(">bbq", 1, 0, 47),       # Magic, flags
            struct.pack(">i2s", 2, b'84'),  # Key is 2 bytes long, '84'
            struct.pack(">i", 2),           # Msg length (bytes)
            b"v3",                          # Message contents

            struct.pack(">q", 0),           # MsgSet offset
            struct.pack(">i", 24),          # MsgSet size
            struct.pack(">i", 100172533),  # CRC
            struct.pack(">bbq", 1, 0, 48),       # Magic, flags
            struct.pack(">i", -1),          # -1 indicates a null key
            struct.pack(">i", 2),           # Msg length (bytes)
            b"v4",                          # Message contents
        ])

        self.assertEqual(decoded, expect)

    def test_encode_message_header(self):
        expect = b"".join([
            struct.pack(">h", 10),              # API Key
            struct.pack(">h", 0),               # API Version
            struct.pack(">i", 4),               # Correlation Id
            struct.pack(">h", len("client3")),  # Length of clientId
            b"client3",                         # ClientId
        ])

        encoded = KafkaCodec._encode_message_header(b"client3", 4, 10)
        self.assertEqual(encoded, expect)

    def test_encode_message(self):
        message = create_message((47, b"test"), b"key", magic=1)
        encoded = KafkaCodec._encode_message(message)
        expect = b"".join([
            struct.pack(">i", -1232077792),  # CRC
            struct.pack(">bbq", 1, 0, 47),        # Magic, flags, timestamp
            struct.pack(">i", 3),            # Length of key
            b"key",                          # key
            struct.pack(">i", 4),            # Length of value
            b"test",                         # value
        ])

        self.assertEqual(encoded, expect)

    def test_decode_message(self):
        encoded = b"".join([
            struct.pack(">i", -1427009701),  # CRC
            struct.pack(">bb", 0, 0),        # Magic, flags
            struct.pack(">i", 3),            # Length of key
            b"key",                          # key
            struct.pack(">i", 4),            # Length of value
            b"test",                         # value
        ])

        offset = 10
        (returned_offset, decoded_message) = list(
            KafkaCodec._decode_message(encoded, offset))[0]

        self.assertEqual(returned_offset, offset)
        self.assertEqual(decoded_message, create_message((-1, b"test"), b"key", magic=0))

    def test_encode_message_failure(self):
        self.assertRaises(ProtocolError,
                          KafkaCodec._encode_message,
                          Message(0, 0, "key", "test"))

    def test_encode_message_set(self):
        message_set = [
            create_message((-1, b"v1"), b"k1"),
            create_message((-1, b"v2"), b"k2"),
        ]
        
        encoded = KafkaCodec._encode_message_set(message_set)
        expect = b"".join([
            struct.pack(">q", 0),           # MsgSet Offset
            struct.pack(">i", 26),          # Msg Size
            struct.pack(">i", -634178223),  # CRC
            struct.pack(">bbq", 1, 0, -1),       # Magic, flags, timestamp
            struct.pack(">i", 2),           # Length of key
            b"k1",                          # Key
            struct.pack(">i", 2),           # Length of value
            b"v1",                          # Value

            struct.pack(">q", 0),          # MsgSet Offset
            struct.pack(">i", 26),         # Msg Size
            struct.pack(">i", 1926397558),  # CRC
            struct.pack(">bbq", 1, 0, -1),      # Magic, flags, timestamp
            struct.pack(">i", 2),          # Length of key
            b"k2",                         # Key
            struct.pack(">i", 2),          # Length of value
            b"v2",                         # Value
        ])

        self.assertEqual(encoded, expect)

    def test_decode_message_set(self):
        encoded = b"".join([
            struct.pack(">q", 0),           # MsgSet Offset
            struct.pack(">i", 18),          # Msg Size
            struct.pack(">i", 1474775406),  # CRC
            struct.pack(">bb", 0, 0),       # Magic, flags
            struct.pack(">i", 2),           # Length of key
            b"k1",                          # Key
            struct.pack(">i", 2),           # Length of value
            b"v1",                          # Value

            struct.pack(">q", 1),          # MsgSet Offset
            struct.pack(">i", 18),         # Msg Size
            struct.pack(">i", -16383415),  # CRC
            struct.pack(">bb", 0, 0),      # Magic, flags
            struct.pack(">i", 2),          # Length of key
            b"k2",                         # Key
            struct.pack(">i", 2),          # Length of value
            b"v2",                         # Value
        ])

        msgs = list(KafkaCodec._decode_message_set_iter(encoded))
        self.assertEqual(len(msgs), 2)
        msg1, msg2 = msgs

        returned_offset1, decoded_message1 = msg1
        returned_offset2, decoded_message2 = msg2

        self.assertEqual(returned_offset1, 0)
        self.assertEqual(decoded_message1, create_message((-1, b"v1"), b"k1", magic=0))

        self.assertEqual(returned_offset2, 1)
        self.assertEqual(decoded_message2, create_message((-1, b"v2"), b"k2", magic=0))

    def test_decode_message_gzip(self):
        gzip_encoded = (b'\xc0\x11\xb2\xf0\x00\x01\xff\xff\xff\xff\x00\x00\x000'
                        b'\x1f\x8b\x08\x00\xa1\xc1\xc5R\x02\xffc`\x80\x03\x01'
                        b'\x9f\xf9\xd1\x87\x18\x18\xfe\x03\x01\x90\xc7Tf\xc8'
                        b'\x80$wu\x1aW\x05\x92\x9c\x11\x00z\xc0h\x888\x00\x00'
                        b'\x00')
        offset = 11
        messages = list(KafkaCodec._decode_message(gzip_encoded, offset))

        self.assertEqual(len(messages), 2)
        msg1, msg2 = messages

        returned_offset1, decoded_message1 = msg1
        self.assertEqual(returned_offset1, 0)
        self.assertEqual(decoded_message1, create_message((-1, b"v1"), magic=0))

        returned_offset2, decoded_message2 = msg2
        self.assertEqual(returned_offset2, 0)
        self.assertEqual(decoded_message2, create_message((-1, b"v2"), magic=0))

    def test_decode_message_snappy(self):
        if not has_snappy():
            raise SkipTest("Snappy not available")  # pragma: no cover
        snappy_encoded = (b'\xec\x80\xa1\x95\x00\x02\xff\xff\xff\xff\x00\x00'
                          b'\x00,8\x00\x00\x19\x01@\x10L\x9f[\xc2\x00\x00\xff'
                          b'\xff\xff\xff\x00\x00\x00\x02v1\x19\x1bD\x00\x10\xd5'
                          b'\x96\nx\x00\x00\xff\xff\xff\xff\x00\x00\x00\x02v2')
        offset = 11
        messages = list(KafkaCodec._decode_message(snappy_encoded, offset))
        self.assertEqual(len(messages), 2)

        msg1, msg2 = messages

        returned_offset1, decoded_message1 = msg1
        self.assertEqual(returned_offset1, 0)
        self.assertEqual(decoded_message1, create_message((-1, b"v1"), magic=0))

        returned_offset2, decoded_message2 = msg2
        self.assertEqual(returned_offset2, 0)
        self.assertEqual(decoded_message2, create_message((-1, b"v2"), magic=0))

    def test_decode_message_checksum_error(self):
        invalid_encoded_message = b"This is not a valid encoded message"
        iter = KafkaCodec._decode_message(invalid_encoded_message, 0)
        self.assertRaises(ChecksumError, list, iter)

    # NOTE: The error handling in _decode_message_set_iter() is questionable.
    # If it's modified, the next two tests might need to be fixed.
    def test_decode_message_set_fetch_size_too_small(self):
        self.assertRaises(ConsumerFetchSizeTooSmall,
                          list, KafkaCodec._decode_message_set_iter('a'))

    def test_decode_message_set_stop_iteration(self):
        encoded = b"".join([
            struct.pack(">q", 0),           # MsgSet Offset
            struct.pack(">i", 18),          # Msg Size
            struct.pack(">i", 1474775406),  # CRC
            struct.pack(">bb", 0, 0),       # Magic, flags
            struct.pack(">i", 2),           # Length of key
            b"k1",                          # Key
            struct.pack(">i", 2),           # Length of value
            b"v1",                          # Value

            struct.pack(">q", 1),           # MsgSet Offset
            struct.pack(">i", 18),          # Msg Size
            struct.pack(">i", -16383415),   # CRC
            struct.pack(">bb", 0, 0),       # Magic, flags
            struct.pack(">i", 2),           # Length of key
            b"k2",                          # Key
            struct.pack(">i", 2),           # Length of value
            b"v2",                          # Value
            b"@1$%(Y!",                     # Random padding
        ])

        msgs = list(KafkaCodec._decode_message_set_iter(encoded))
        self.assertEqual(len(msgs), 2)
        msg1, msg2 = msgs

        returned_offset1, decoded_message1 = msg1
        returned_offset2, decoded_message2 = msg2

        self.assertEqual(returned_offset1, 0)
        self.assertEqual(decoded_message1, create_message((-1, b"v1"), b"k1", magic=0))

        self.assertEqual(returned_offset2, 1)
        self.assertEqual(decoded_message2, create_message((-1, b"v2"), b"k2", magic=0))

    def test_get_response_correlation_id(self):
        t1 = b"topic1"
        t2 = b"topic2"
        corrID = 30372
        encoded = struct.pack('>iih%dsiihqihqh%dsiihq' % (len(t1), len(t2)),
                              corrID, 2, len(t1), t1, 2, 0, 0, 10, 1, 1, 20,
                              len(t2), t2, 1, 0, 0, 30)
        self.assertEqual(
            corrID, KafkaCodec.get_response_correlation_id(encoded))

    def test_encode_produce_request(self):
        requests = [
            ProduceRequest("topic1", 0, [
                create_message((-1, b"a")),
                create_message((-1, b"b")),
            ]),
            ProduceRequest(u"topic2", 1, [
                create_message((-1, b"c")),
            ]),
        ]

        msg_a_binary = KafkaCodec._encode_message(create_message((-1, b"a")))
        msg_b_binary = KafkaCodec._encode_message(create_message((-1, b"b")))
        msg_c_binary = KafkaCodec._encode_message(create_message((-1, b"c")))

        header = b"".join([
            struct.pack('>h', 0),        # Msg Header, Message type = Produce
            struct.pack('>h', 1),        # Msg Header, API version
            struct.pack('>i', 2),        # Msg Header, Correlation ID
            struct.pack('>h7s', 7, b"client1"),  # Msg Header, The client ID
            struct.pack('>h', 2),        # Num acks required
            struct.pack('>i', 100),      # Request Timeout
            struct.pack('>i', 2),        # The number of requests
        ])

        total_len = len(msg_a_binary) + len(msg_b_binary)
        topic1 = b"".join([
            struct.pack('>h6s', 6, b'topic1'),         # The topic1
            struct.pack('>i', 1),                      # One message set
            struct.pack('>i', 0),                      # Partition 0
            struct.pack('>i', total_len + 24),         # Size of message set
            struct.pack('>q', 0),                      # No offset specified
            struct.pack('>i', len(msg_a_binary)),      # Length of message
            msg_a_binary,                              # Actual message
            struct.pack('>q', 0),                      # No offset specified
            struct.pack('>i', len(msg_b_binary)),      # Length of message
            msg_b_binary,                              # Actual message
        ])

        topic2 = b"".join([
            struct.pack('>h6s', 6, b'topic2'),          # The topic1
            struct.pack('>i', 1),                       # One message set
            struct.pack('>i', 1),                       # Partition 1
            struct.pack('>i', len(msg_c_binary) + 12),  # Size of message set
            struct.pack('>q', 0),                       # No offset specified
            struct.pack('>i', len(msg_c_binary)),       # Length of message
            msg_c_binary,                               # Actual message
        ])

        expected1 = b"".join([header, topic1, topic2])
        expected2 = b"".join([header, topic2, topic1])

        encoded = KafkaCodec.encode_produce_request(
            b"client1", 2, requests, 2, 100)

        self.assertIn(encoded, [expected1, expected2])

    def test_decode_produce_response(self):
        t1 = "topic1"
        t2 = u"topic2"
        encoded = struct.pack('>iih%dsiihqlihqlh%dsiihql' % (len(t1), len(t2)),
                              2, 2, len(t1), t1.encode(), 2, 0, 0, 10, 4, 1, 1, 20, 5,
                              len(t2), t2.encode(), 1, 0, 0, 30, 6)
        responses = list(KafkaCodec.decode_produce_response(encoded))
        self.assertEqual(responses,
                         [ProduceResponse(t1, 0, 0, 10, 4),
                          ProduceResponse(t1, 1, 1, 20, 5),
                          ProduceResponse(t2, 0, 0, 30, 6)])

    def test_encode_fetch_request(self):
        requests = [
            FetchRequest("topic1", 0, 10, 1024),
            FetchRequest(u"topic2", 1, 20, 100),
        ]

        header = b"".join([
            struct.pack('>h', 1),       # Msg Header, Message type = Fetch
            struct.pack('>h', 0),               # Msg Header, API version
            struct.pack('>i', 3),               # Msg Header, Correlation ID
            struct.pack('>h7s', 7, b"client1"),  # Msg Header, The client ID
            struct.pack('>i', -1),              # Replica Id
            struct.pack('>i', 2),               # Max wait time
            struct.pack('>i', 100),             # Min bytes
            struct.pack('>i', 2),               # Num requests
        ])

        topic1 = b"".join([
            struct.pack('>h6s', 6, b'topic1'),  # Topic
            struct.pack('>i', 1),              # Num Payloads
            struct.pack('>i', 0),              # Partition 0
            struct.pack('>q', 10),             # Offset
            struct.pack('>i', 1024),           # Max Bytes
        ])

        topic2 = b"".join([
            struct.pack('>h6s', 6, b'topic2'),  # Topic
            struct.pack('>i', 1),              # Num Payloads
            struct.pack('>i', 1),              # Partition 0
            struct.pack('>q', 20),             # Offset
            struct.pack('>i', 100),            # Max Bytes
        ])

        expected1 = b"".join([header, topic1, topic2])
        expected2 = b"".join([header, topic2, topic1])

        encoded = KafkaCodec.encode_fetch_request(
            b"client1", 3, requests, 2, 100)
        self.assertIn(encoded, [expected1, expected2])

    def test_decode_fetch_response(self):
        t1 = "topic1"
        t2 = u"topic2"
        msgs = [create_message(m) for m in [b"message1", b"hi", b"boo", b"foo", b"so fun!"]]
        ms1 = KafkaCodec._encode_message_set([msgs[0], msgs[1]])
        ms2 = KafkaCodec._encode_message_set([msgs[2]])
        ms3 = KafkaCodec._encode_message_set([msgs[3], msgs[4]])

        encoded = struct.pack('>iih%dsiihqi%dsihqi%dsh%dsiihqi%ds' %
                              (len(t1), len(ms1), len(ms2), len(t2), len(ms3)),
                              4, 2, len(t1), t1.encode(), 2, 0, 1, 10, len(ms1), ms1, 1,
                              1, 20, len(ms2), ms2, len(t2), t2.encode(), 1, 0, 1, 30,
                              len(ms3), ms3)

        responses = list(KafkaCodec.decode_fetch_response(encoded))

        def expand_messages(response):
            return FetchResponse(response.topic, response.partition,
                                 response.error, response.highwaterMark,
                                 list(response.messages))

        expanded_responses = [expand_messages(r) for r in responses]
        expect = [FetchResponse(t1, 0, 0, 10, [OffsetAndMessage(0, msgs[0]),
                                               OffsetAndMessage(0, msgs[1])]),
                  FetchResponse(t1, 1, 1, 20, [OffsetAndMessage(0, msgs[2])]),
                  FetchResponse(t2, 0, 0, 30, [OffsetAndMessage(0, msgs[3]),
                                               OffsetAndMessage(0, msgs[4])])]
        self.assertEqual(expanded_responses, expect)

    def test_encode_metadata_request_no_topics(self):
        expected = b"".join([
            struct.pack('>h', 3),           # API key metadata fetch
            struct.pack('>h', 0),           # API version
            struct.pack('>i', 4),           # Correlation ID
            struct.pack('>h3s', 3, b"cid"),  # The client ID
            struct.pack('>i', 0),           # No topics, give all the data!
        ])

        encoded = KafkaCodec.encode_metadata_request(b"cid", 4)

        self.assertEqual(encoded, expected)

    def test_encode_metadata_request_with_topics(self):
        expected = b"".join([
            struct.pack('>h', 3),           # API key metadata fetch
            struct.pack('>h', 0),           # API version
            struct.pack('>i', 4),           # Correlation ID
            struct.pack('>h3s', 3, b"cid"),  # The client ID
            struct.pack('>i', 2),           # Number of topics in the request
            struct.pack('>h2s', 2, b"t1"),   # Topic "t1"
            struct.pack('>h2s', 2, b"t2"),   # Topic "t2"
        ])

        encoded = KafkaCodec.encode_metadata_request(b"cid", 4, [u"t1", "t2"])

        self.assertEqual(encoded, expected)

    def test_decode_metadata_response(self):
        node_brokers = {
            0: BrokerMetadata(0, "brokers1.afkak.rdio.com", 1000),
            1: BrokerMetadata(1, "brokers1.afkak.rdio.com", 1001),
            3: BrokerMetadata(3, "brokers2.afkak.rdio.com", 1000),
        }

        topic_partitions = {
            "topic1": TopicMetadata(
                'topic1', 0, {
                    0: PartitionMetadata(u"topic1", 0, 0, 1, (0, 2), (2,)),
                    1: PartitionMetadata("topic1", 1, 1, 3, (0, 1), (0, 1)),
                },
            ),
            u"topic2": TopicMetadata(
                u'topic2', 1, {
                    0: PartitionMetadata(u"topic2", 0, 0, 0, (), ()),
                },
            ),
        }
        encoded = create_encoded_metadata_response(
            node_brokers, topic_partitions)
        decoded = KafkaCodec.decode_metadata_response(encoded)
        self.assertEqual(decoded, (node_brokers, topic_partitions))

    def test_decode_metadata_response_failures(self):
        data = b"".join([
            struct.pack(">ii", 0xCAFE, 0xBABE),  # correlation ID & numbrokers
            struct.pack('>i', 1234),             # Node Id
            struct.pack('>h', len(b"hostname")), b"hostname",  # host
            struct.pack('>i', 1025),             # port
        ])
        self.assertRaises(InvalidMessageError,
                          KafkaCodec.decode_metadata_response, data)

    def test_encode_offset_request(self):
        expected = b"".join([
            struct.pack('>h', 2),            # Message type = offset fetch
            struct.pack('>h', 0),            # API version
            struct.pack('>i', 4),            # Correlation ID
            struct.pack('>h3s', 3, b"cid"),  # The client ID
            struct.pack('>i', -1),           # Replica Id
            struct.pack('>i', 0),            # No topic/partitions
        ])

        encoded = KafkaCodec.encode_offset_request(b"cid", 4)

        self.assertEqual(encoded, expected)

    def test_encode_offset_request__no_payload(self):
        expected = b"".join([
            struct.pack('>h', 2),             # Message type = offset fetch
            struct.pack('>h', 0),             # API version
            struct.pack('>i', 4),             # Correlation ID
            struct.pack('>h3s', 3, b"cid"),   # The client ID
            struct.pack('>i', -1),            # Replica Id
            struct.pack('>i', 1),             # Num topics
            struct.pack(">h6s", 6, b"topic1"),  # Topic for the request
            struct.pack(">i", 2),             # Two partitions

            struct.pack(">i", 3),             # Partition 3
            struct.pack(">q", -1),            # No time offset
            struct.pack(">i", 1),             # One offset requested

            struct.pack(">i", 4),             # Partition 3
            struct.pack(">q", -1),            # No time offset
            struct.pack(">i", 1),             # One offset requested
        ])

        encoded = KafkaCodec.encode_offset_request(b"cid", 4, [
            OffsetRequest('topic1', 3, -1, 1),
            OffsetRequest(u'topic1', 4, -1, 1),
        ])

        self.assertEqual(encoded, expected)

    def test_decode_offset_response(self):
        encoded = b"".join([
            struct.pack(">i", 42),            # Correlation ID
            struct.pack(">i", 1),             # One topics
            struct.pack(">h6s", 6, b"topic1"),  # First topic
            struct.pack(">i", 2),             # Two partitions

            struct.pack(">i", 2),             # Partition 2
            struct.pack(">h", 0),             # No error
            struct.pack(">i", 1),             # One offset
            struct.pack(">q", 4),             # Offset 4

            struct.pack(">i", 4),             # Partition 4
            struct.pack(">h", 0),             # No error
            struct.pack(">i", 1),             # One offset
            struct.pack(">q", 8),             # Offset 8
        ])

        results = KafkaCodec.decode_offset_response(encoded)
        self.assertEqual(set(results), set([
            OffsetResponse(topic='topic1', partition=2, error=0, offsets=(4,)),
            OffsetResponse(topic=u'topic1', partition=4, error=0, offsets=(8,)),
        ]))

    def test_encode_offset_commit_request(self):
        header = b"".join([
            struct.pack('>h', 8),                # Message type = offset commit
            struct.pack('>h', 1),                # API version
            struct.pack('>i', 42),               # Correlation ID
            struct.pack('>h9s', 9, b"client_id"),  # The client ID
            struct.pack('>h8s', 8, b"group_id"),  # The group to commit for
            struct.pack('>i', 996),              # Group generation ID
            struct.pack('>h11s', 11, b'consumer_id'),  # Consumer ID
            struct.pack('>i', 2),                # Num topics
        ])

        topic1 = b"".join([
            struct.pack(">h6s", 6, b"topic1"),    # Topic for the request
            struct.pack(">i", 2),                # Two partitions
            struct.pack(">i", 0),                # Partition 0
            struct.pack(">q", 123),              # Offset 123
            struct.pack(">q", 1437585816816),    # Timestamp in ms > epoch
            struct.pack(">h", -1),               # Null metadata
            struct.pack(">i", 1),                # Partition 1
            struct.pack(">q", 234),              # Offset 234
            struct.pack(">q", 1436981054199),    # Timestamp in ms > epoch
            struct.pack(">h11s", 11, b'My_Metadata'),  # Null metadata
        ])

        topic2 = b"".join([
            struct.pack(">h6s", 6, b"topic2"),    # Topic for the request
            struct.pack(">i", 1),                # One partition
            struct.pack(">i", 2),                # Partition 2
            struct.pack(">q", 345),              # Offset 345
            struct.pack(">q", -1),               # Timestamp 'invalid-time'
            struct.pack(">h", -1),               # Null metadata
        ])

        # A dict is used, so we can't predict the order of the topics...
        expected1 = b"".join([header, topic1, topic2])
        expected2 = b"".join([header, topic2, topic1])

        encoded = KafkaCodec.encode_offset_commit_request(
            b"client_id", 42, u"group_id", 996, u'consumer_id', [
                OffsetCommitRequest("topic1", 0, 123, 1437585816816, None),
                OffsetCommitRequest(u"topic1", 1, 234, 1436981054199,
                                    b'My_Metadata'),
                OffsetCommitRequest(u"topic2", 2, 345, -1, None),
            ])

        self.assertIn(encoded, [expected1, expected2])

    def test_decode_offset_commit_response(self):
        encoded = b"".join([
            struct.pack(">i", 42),            # Correlation ID
            struct.pack(">i", 1),             # One topic
            struct.pack(">h6s", 6, b"topic1"),  # First topic
            struct.pack(">i", 2),             # Two partitions

            struct.pack(">i", 2),             # Partition 2
            struct.pack(">h", 0),             # No error

            struct.pack(">i", 4),             # Partition 4
            struct.pack(">h", 0),             # No error
        ])

        results = KafkaCodec.decode_offset_commit_response(encoded)
        self.assertEqual(set(results), set([
            OffsetCommitResponse(topic='topic1', partition=2, error=0),
            OffsetCommitResponse(topic=u'topic1', partition=4, error=0),
        ]))

    def test_encode_offset_fetch_request(self):
        header = b"".join([
            struct.pack('>h', 9),                  # Message type = offset fetch
            struct.pack('>h', 1),                  # API version
            struct.pack('>i', 42),                 # Correlation ID
            struct.pack('>h9s', 9, b"client_id"),  # The client ID
            struct.pack('>h8s', 8, b"group_id"),   # The group to commit for
            struct.pack('>i', 2),                  # Num topics
        ])

        topic1 = b"".join([
            struct.pack(">h6s", 6, b"topic1"),   # Topic for the request
            struct.pack(">i", 2),                # Two partitions
            struct.pack(">i", 0),                # Partition 0
            struct.pack(">i", 1),                # Partition 1
        ])

        topic2 = b"".join([
            struct.pack(">h6s", 6, b"topic2"),   # Topic for the request
            struct.pack(">i", 1),                # One partitions
            struct.pack(">i", 2),                # Partition 2
        ])

        expected1 = b"".join([header, topic1, topic2])
        expected2 = b"".join([header, topic2, topic1])

        encoded = KafkaCodec.encode_offset_fetch_request(
            b"client_id", 42, u"group_id", [
                OffsetFetchRequest("topic1", 0),
                OffsetFetchRequest(u"topic1", 1),
                OffsetFetchRequest("topic2", 2),
            ])

        self.assertIn(encoded, [expected1, expected2])

    def test_decode_offset_fetch_response(self):
        encoded = b"".join([
            struct.pack(">i", 42),              # Correlation ID
            struct.pack(">i", 1),               # One topics
            struct.pack(">h6s", 6, b"topic1"),  # First topic
            struct.pack(">i", 2),               # Two partitions

            struct.pack(">i", 2),             # Partition 2
            struct.pack(">q", 4),             # Offset 4
            struct.pack(">h4s", 4, b"meta"),  # Metadata
            struct.pack(">h", 0),             # No error

            struct.pack(">i", 4),             # Partition 4
            struct.pack(">q", 8),             # Offset 8
            struct.pack(">h4s", 4, b"meta"),  # Metadata
            struct.pack(">h", 0),             # No error
        ])

        results = KafkaCodec.decode_offset_fetch_response(encoded)
        self.assertEqual(set(results), set([
            OffsetFetchResponse(topic=u'topic1', partition=2, offset=4,
                                error=0, metadata=b"meta"),
            OffsetFetchResponse(topic='topic1', partition=4, offset=8,
                                error=0, metadata=b"meta"),
        ]))

    @contextmanager
    def mock_create_message_fns(self):
        p1 = mock.patch.object(afkak.kafkacodec, "create_message",
                               return_value=sentinel.message)
        p2 = mock.patch.object(afkak.kafkacodec, "create_gzip_message",
                               return_value=sentinel.gzip_message)
        p3 = mock.patch.object(afkak.kafkacodec, "create_snappy_message",
                               return_value=sentinel.snappy_message)
        with p1, p2, p3:
            yield

    def test_create_message_set(self):
        messages = [b'1', b'2', b'3']
        reqs = make_send_requests(messages)

        # Default codec is CODEC_NONE. Expect list of regular messages.
        expect = [sentinel.message] * len(messages)
        with self.mock_create_message_fns():
            message_set = create_message_set(reqs)
        self.assertEqual(message_set, expect)

        # CODEC_NONE: Expect list of regular messages.
        expect = [sentinel.message] * len(messages)
        with self.mock_create_message_fns():
            message_set = create_message_set(reqs, CODEC_NONE)
        self.assertEqual(message_set, expect)

        # CODEC_GZIP: Expect list of one gzip-encoded message.
        expect = [sentinel.gzip_message]
        with self.mock_create_message_fns():
            message_set = create_message_set(reqs, CODEC_GZIP)
        self.assertEqual(message_set, expect)

        # CODEC_SNAPPY: Expect list of one snappy-encoded message.
        expect = [sentinel.snappy_message]
        with self.mock_create_message_fns():
            message_set = create_message_set(reqs, CODEC_SNAPPY)
        self.assertEqual(message_set, expect)

        # Unknown codec should raise UnsupportedCodecError.
        self.assertRaises(UnsupportedCodecError,
                          create_message_set, reqs, -1)

    def test_encode_consumer_metadata_request(self):
        expected = b"".join([
            struct.pack('>h', 10),          # API key ConsumerMetadataRequest
            struct.pack('>h', 0),           # API version
            struct.pack('>i', 9),           # Correlation ID
            struct.pack('>h3s', 3, b"cID"),  # The client ID
            struct.pack('>h6s', 6, b"group1"),   # Consumer group 'group1'
        ])

        encoded = KafkaCodec.encode_consumermetadata_request(b"cID", 9, u"group1")

        self.assertEqual(encoded, expected)

    def test_decode_consumermetadata_response(self):
        expected = ConsumerMetadataResponse(0, 5, "theHost", 4242)
        encoded = b"".join([
            struct.pack('>i', 9),  # Correlation ID
            struct.pack('>h', 0),  # Error Code
            struct.pack('>i', 5),  # Coordinator ID
            struct.pack('>h', len(b"theHost")), b"theHost",  # Coordinator Host
            struct.pack('>i', 4242),  # Coordinator port
        ])
        decoded = KafkaCodec.decode_consumermetadata_response(encoded)
        self.assertEqual(decoded, expected)
