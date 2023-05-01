# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2019, 2021 Ciena Corporation
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
Test code for Partitioner, RoundRobinPartitioner, and HashedPartitioner
classes.
"""

import logging
from collections import defaultdict
from math import sqrt
from unittest import TestCase

from afkak.partitioner import HashedPartitioner, Partitioner, RoundRobinPartitioner, pure_murmur2

from .testutil import random_string

log = logging.getLogger(__name__)


# Re-implement std so we don't need numpy just for a couple of tests,
# since it wouldn't build in the pypy tox environment
def std(data):
    if not len(data):
        return float("nan")  # pragma: no cover
    mean = sum([float(x) for x in data]) / len(data)
    var = sum([(mean - x) ** 2 for x in data]) / len(data)
    return sqrt(var)


class TestPartitioner(TestCase):
    def test_constructor(self):
        topic = "TheTestTopic"
        parts = [1, 2, 3, 4, 5, 6]
        p = Partitioner(topic, parts)
        self.assertEqual(topic, p.topic)
        self.assertEqual(parts, p.partitions)

    def test_partition(self):
        parts = [1, 2, 3, 4, 5, 6]
        p = Partitioner(None, parts)

        self.assertRaises(NotImplementedError, p.partition, "key", parts)


class TestRoundRobinPartitioner(TestCase):
    def test_constructor(self):
        parts = [1, 2, 3, 4, 5, 6]
        p = RoundRobinPartitioner(None, parts)
        self.assertEqual(parts, p.partitions)

    def test_repr(self):
        parts = [1, 3, 5, 7]
        p = RoundRobinPartitioner(None, parts)
        self.assertEqual(p.__repr__(), "<RoundRobinPartitioner False:[1, 3, 5, 7]>")

    def test_partition(self):
        parts = [1, 2, 3, 4, 5, 6]
        p = RoundRobinPartitioner(None, parts)

        for part in parts:
            self.assertEqual(part, p.partition("key", parts))

        for part in parts:
            self.assertEqual(part, p.partition("key", parts))

        # Make sure updates work
        parts = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        for part in parts:
            self.assertEqual(part, p.partition("key", parts))

        for part in parts:
            self.assertEqual(part, p.partition("key", parts))

    def test_set_random_start(self):
        RoundRobinPartitioner.set_random_start(True)
        self.assertTrue(RoundRobinPartitioner.randomStart)
        parts = [1, 2, 3, 4, 5, 6, 7, 8, 19, 20, 30]
        # Try a number of times and check the distribution of the start
        firstParts = defaultdict(lambda: 0)
        trycount = 10000
        for _i in range(trycount):
            p1 = RoundRobinPartitioner(None, parts)
            firstParts[p1.partition(None, parts)] += 1

        self.assertLess(std(firstParts.values()), trycount / 100)

        RoundRobinPartitioner.set_random_start(False)
        self.assertFalse(RoundRobinPartitioner.randomStart)
        p2 = RoundRobinPartitioner(None, parts)
        self.assertFalse(p2.randomStart)
        for part in parts:
            self.assertEqual(part, p2.partition(None, parts))


class TestHashedPartitioner(TestCase):
    """TestHashedPartitioner

    For all these tests that compare the result, they were performed also
    with a Java program which matches the Kafka Java client to determine the
    values to match against.
    """

    T1 = "T1"
    parts = range(100000)  # big enough have low probability of collision

    def test_key_none(self):
        """
        A key of None results in a TypeError as it doesn't make sense to
        produce unkeyed messages with a hashed partitioner.
        """
        key = None
        p = HashedPartitioner(self.T1, self.parts)
        self.assertRaises(TypeError, p.partition, key, self.parts)

    def test_key_str(self):
        """
        Bytestring keys are hashed directly.
        """
        key = b"The rain in Spain falls mainly on the plain."
        expected = (2823782121 & 0x7FFFFFFF) % 100000  # Int is from murmur2
        p = HashedPartitioner(self.T1, self.parts)
        part = p.partition(key, self.parts)
        self.assertEqual(expected, part)

    def test_key_unicode(self):
        """
        Text string keys are converted to UTF-8 and hashed to match the Java
        implementation.
        """
        key = "슬듢芬"
        expected = (3978338664 & 0x7FFFFFFF) % 100000  # Int is from murmur2
        p = HashedPartitioner(self.T1, self.parts)
        part = p.partition(key, self.parts)
        self.assertEqual(expected, part)

    def test_key_integer(self):
        """
        Integer keys produce a TypeError as passing an integer likely
        represents a programming error.
        """
        key = 123456789
        p = HashedPartitioner(self.T1, self.parts)
        self.assertRaises(TypeError, p.partition, key, self.parts)

    def test_key_bytearray(self):
        """
        :class:`bytearray` keys are hashed directly.
        """
        # 15 letters long to hit 'if extrabytes == 3:'
        key = bytearray(b"lasquinceletras")
        expected = (4030895744 & 0x7FFFFFFF) % 100000  # Int is from murmur2
        p = HashedPartitioner(self.T1, self.parts)
        part = p.partition(key, self.parts)
        self.assertEqual(expected, part)

    def test_key_large(self):
        """
        The hashed partitioner does not limit key length.
        """
        key = "".join(["Key:{} ".format(i) for i in range(4096)]).encode("ascii")
        expected = 1765856722 % 100000  # Int is from online hash tool
        p = HashedPartitioner(self.T1, self.parts)
        part = p.partition(key, self.parts)
        self.assertEqual(expected, part)

    def test_key_match_java(self):
        """
        Hashing a UUID produces the same result the Java implementation would.
        """
        key = b"cc54d7f5-8508-4302-bc23-c5d16cfb50fd"
        key = key.decode(encoding="UTF-8", errors="strict")
        parts = range(10)
        expected = 4
        p = HashedPartitioner(self.T1, parts)
        part = p.partition(key, parts)
        self.assertEqual(expected, part)

    def test_partition_distribution(self):
        parts = [1, 2, 3, 4, 5]
        p = HashedPartitioner(self.T1, parts)

        # Make sure we have decent distribution
        keycount = 10000
        key_list = []
        part_keycount = defaultdict(lambda: 0)
        key_to_part = {}
        for _i in range(keycount):
            key = random_string(16)
            key_list.append(key)
            part = p.partition(key, parts)
            part_keycount[part] += 1
            key_to_part[key] = part

        self.assertLess(std(part_keycount.values()), keycount / 100)

        # Ensure we get the same partition for the same key
        for key in key_to_part.keys():
            part = p.partition(key, parts)
            self.assertEqual(part, key_to_part[key])


class TestPureMurmur2(TestCase):
    def test_pure_murmur2(self):
        data = [b"", b"testing", b"PEACH!", b"Gorz!", b"987654321", b"_!!_", b"CRINOID"]
        expect = [
            275646681,
            2291530147,
            2546348827,
            1742407956,
            577579727,
            2335345241,
            3603193626,
        ]
        for d, e in zip(data, expect):
            self.assertEqual(e, pure_murmur2(bytearray(d)))

    def test_pure_murmur2_badarg(self):
        # pure_murmur2 wants bytearray, not string
        self.assertRaises(TypeError, pure_murmur2, "BadArg")
