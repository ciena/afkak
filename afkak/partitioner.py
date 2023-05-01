# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2017, 2018, 2019, 2020 Ciena Corporation
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

import warnings
from itertools import cycle
from random import randint

try:
    from murmurhash2 import murmurhash2

    _c_murmur2 = murmurhash2(b"", 0x9747B28C)
except ImportError:  # pragma: no cover
    _c_murmur2 = None


def pure_murmur2(byte_array, seed=0x9747B28C):
    """Pure-python Murmur2 implementation.

    Based on java client, see org.apache.kafka.common.utils.Utils.murmur2
    https://github.com/apache/kafka/blob/0.8.2/clients/src/main/java/org/apache/kafka/common/utils/Utils.java#L244
    Args:
        byte_array: bytearray - Raises TypeError otherwise

    Returns: MurmurHash2 of byte_array bytearray
    Raises: TypeError if byte_array arg is not of type bytearray
    """
    # Ensure byte_array arg is a bytearray
    if not isinstance(byte_array, bytearray):
        raise TypeError("Type: %r of 'byte_array' arg must be 'bytearray'", type(byte_array))

    length = len(byte_array)
    # 'm' and 'r' are mixing constants generated offline.
    # They're not really 'magic', they just happen to work well.
    m = 0x5BD1E995
    r = 24
    mod32bits = 0xFFFFFFFF

    # Initialize the hash to a random value
    h = seed ^ length
    length4 = length // 4

    for i in range(length4):
        i4 = i * 4
        k = (
            (byte_array[i4 + 0] & 0xFF)
            + ((byte_array[i4 + 1] & 0xFF) << 8)
            + ((byte_array[i4 + 2] & 0xFF) << 16)
            + ((byte_array[i4 + 3] & 0xFF) << 24)
        )
        k &= mod32bits
        k *= m
        k &= mod32bits
        k ^= (k % 0x100000000) >> r  # k ^= k >>> r
        k &= mod32bits
        k *= m
        k &= mod32bits

        h *= m
        h &= mod32bits
        h ^= k
        h &= mod32bits

    # Handle the last few bytes of the input array
    extra_bytes = length % 4
    if extra_bytes == 3:
        h ^= (byte_array[(length & ~3) + 2] & 0xFF) << 16
        h &= mod32bits

    if extra_bytes >= 2:
        h ^= (byte_array[(length & ~3) + 1] & 0xFF) << 8
        h &= mod32bits

    if extra_bytes >= 1:
        h ^= byte_array[length & ~3] & 0xFF
        h &= mod32bits
        h *= m
        h &= mod32bits

    h ^= (h % 0x100000000) >> 13  # h >>> 13;
    h &= mod32bits
    h *= m
    h &= mod32bits
    h ^= (h % 0x100000000) >> 15  # h >>> 15;
    h &= mod32bits

    return h


class Partitioner(object):
    """
    Base class for a partitioner

    :ivar bytes topic: Topic name
    :ivar partitions: :class:`list` of :class:`int`
    """

    def __init__(self, topic, partitions):
        """
        Initialize the partitioner

        partitions - A list of available partitions (during startup)
        """
        self.topic = topic
        self.partitions = partitions

    def partition(self, key, partitions):
        """
        Takes a key (string) and partitions (list) as argument and returns
        a partition to be used for the message

        partitions - The list of partitions is passed in every call. This
                     may look like an overhead, but it will be useful
                     (in future) when we handle cases like rebalancing
        """
        raise NotImplementedError("partition function has to be implemented")


class RoundRobinPartitioner(Partitioner):
    """
    Implements a round robin partitioner which sends data to partitions
    in a round robin fashion. Also supports starting each new partitioner
    at a random offset into the cycle of partitions
    """

    randomStart = False

    @classmethod
    def set_random_start(cls, randomStart):
        cls.randomStart = randomStart

    def __init__(self, topic, partitions):
        super(RoundRobinPartitioner, self).__init__(topic, partitions)
        self._set_partitions(partitions)

    def __repr__(self):
        return "<RoundRobinPartitioner {}:{}>".format(self.randomStart, self.partitions)

    def _set_partitions(self, partitions):
        self.partitions = sorted(partitions)
        self.iterpart = cycle(partitions)
        if self.randomStart:
            for _ in range(randint(0, len(partitions) - 1)):
                next(self.iterpart)

    def partition(self, key, partitions):
        # Refresh the partition list if necessary
        if self.partitions != partitions:
            self._set_partitions(partitions)
        return next(self.iterpart)


class HashedPartitioner(Partitioner):
    """
    Implements a partitioner which selects the target partition based on
    the hash of the key.
    """

    if _c_murmur2:

        def _hash(self, key):
            """
            Coerce the input into `bytes` with UTF-8 encoding for the
            C MurmurHash2 implementation.
            """
            if isinstance(key, type("")):
                key = key.encode("UTF-8")
            elif isinstance(key, bytearray):
                key = bytes(key)
            elif not isinstance(key, bytes):
                raise TypeError(
                    "Partition key {!r} must be {} or {}," " not {}".format(key, type(b""), type(""), type(key))
                )
            return murmurhash2(key, 0x9747B28C)

    else:

        def _hash(self, key):
            """
            Coerce the input into a bytearray for the pure-Python MurmurHash2
            implementation.
            """
            warnings.warn(
                "Using slow pure Python Murmur2 hash; install Afkak[FastMurmur2] for speedup",
                ImportWarning,
            )
            if isinstance(key, type("")):
                key = bytearray(key, "UTF-8")
            elif isinstance(key, bytes):
                key = bytearray(key)
            elif not isinstance(key, bytearray):
                raise TypeError("Partition key {!r} must be str, bytes, or bytearray, not {}".format(key, type(key)))
            return pure_murmur2(key)

    def partition(self, key, partitions):
        """
        Select a partition based on the hash of the key.

        :param key: Partition key
        :type key: text string or UTF-8 `bytes` or `bytearray`
        :param list partitions:
            An indexed sequence of partition identifiers.
        :returns:
            One of the given partition identifiers. The result will be the same
            each time the same key and partition list is passed.
        """
        return partitions[(self._hash(key) & 0x7FFFFFFF) % len(partitions)]
