# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2017, 2018, 2021 Ciena Corporation.
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

import collections
import struct

from .common import BufferUnderflowError

_NULL_SHORT_STRING = struct.pack(">h", -1)


def _buffer_underflow(what, buf, offset, size):
    return BufferUnderflowError(
        (
            "Not enough data to read {what} at offset {offset:,d}: {size:,d} bytes required,"
            " but {available:,d} available."
        ).format(
            what=what,
            offset=offset,
            size=size,
            available=len(buf) - offset,
        )
    )


def _coerce_topic(topic):
    """
    Ensure that the topic name is text string of a valid length.

    :param topic: Kafka topic name. Valid characters are in the set ``[a-zA-Z0-9._-]``.
    :raises ValueError: when the topic name exceeds 249 bytes
    :raises TypeError: when the topic is not :class:`unicode` or :class:`str`
    """
    if not isinstance(topic, str):
        raise TypeError("topic={!r} must be text".format(topic))
    if not isinstance(topic, str):
        topic = topic.decode("ascii")
    if len(topic) < 1:
        raise ValueError("invalid empty topic name")
    if len(topic) > 249:
        raise ValueError("topic={!r} name is too long: {} > 249".format(topic, len(topic)))
    return topic


def _coerce_consumer_group(consumer_group):
    """
    Ensure that the consumer group is a text string.

    :param consumer_group: :class:`bytes` or :class:`str` instance
    :raises TypeError: when `consumer_group` is not :class:`bytes`
        or :class:`str`
    """
    if not isinstance(consumer_group, (str, bytes)):
        raise TypeError("consumer_group={!r} must be text".format(consumer_group))
    if not isinstance(consumer_group, str):
        consumer_group = consumer_group.decode("utf-8")
    return consumer_group


def _coerce_client_id(client_id):
    """
    Ensure the provided client ID is a byte string. If a text string is
    provided, it is encoded as UTF-8 bytes.

    :param client_id: :class:`bytes` or :class:`str` instance
    """
    if isinstance(client_id, type("")):
        client_id = client_id.encode("utf-8")
    if not isinstance(client_id, bytes):
        raise TypeError("{!r} is not a valid consumer group (must be" " str or bytes)".format(client_id))
    return client_id


def write_int_string(s):
    if s is None:
        return struct.pack(">i", -1)
    return struct.pack(">i", len(s)) + s


def write_short_ascii(s):
    """
    Encode a Kafka short string which represents ASCII text.

    :param str s:
        Text string or ``None``. The string will be ASCII-encoded.

    :returns: length-prefixed `bytes`
    :raises:
        `struct.error` for strings longer than 32767 characters
    """
    if s is None:
        return _NULL_SHORT_STRING
    if not isinstance(s, str):
        raise TypeError("{!r} is not text".format(s))
    return write_short_bytes(s.encode("ascii"))


def write_short_text(s):
    """
    Encode a Kafka short string which represents Unicode text.

    :param str s:
        Text string or ``None``. The string will be UTF-8 encoded.

    :returns: length-prefixed `bytes`
    :raises:
        `struct.error` when the UTF-8 encoded form of the string exceeds
        32767 bytes.
    """
    if s is None:
        return _NULL_SHORT_STRING
    if not isinstance(s, str):
        raise TypeError("{!r} is not text".format(s))
    return write_short_bytes(s.encode("utf-8"))


def write_short_bytes(b):
    """
    Encode a Kafka short string which contains arbitrary bytes. A short string
    is limited to 32767 bytes in length by the signed 16-bit length prefix.
    A length prefix of -1 indicates ``null``, represented as ``None`` in
    Python.

    :param bytes b:
        No more than 32767 bytes, or ``None`` for the null encoding.
    :return: length-prefixed `bytes`
    :raises:
        `struct.error` for strings longer than 32767 characters
    """
    if b is None:
        return _NULL_SHORT_STRING
    if not isinstance(b, bytes):
        raise TypeError("{!r} is not bytes".format(b))
    elif len(b) > 32767:
        raise struct.error(len(b))
    else:
        return struct.pack(">h", len(b)) + b


def read_short_bytes(data, cur):
    if len(data) < cur + 2:
        raise _buffer_underflow("short string length", data, cur, 2)

    (strlen,) = struct.unpack(">h", data[cur : cur + 2])
    if strlen == -1:
        return None, cur + 2

    cur += 2
    if len(data) < cur + strlen:
        raise _buffer_underflow("short string", data, cur, strlen)

    out = data[cur : cur + strlen]
    return out, cur + strlen


def read_short_ascii(data, cur):
    b, cur = read_short_bytes(data, cur)
    return b.decode("ascii"), cur


def read_short_text(data, cur):
    b, cur = read_short_bytes(data, cur)
    return b.decode("utf-8"), cur


def read_int_string(data, cur):
    if len(data) < cur + 4:
        raise _buffer_underflow("long string length", data, cur, 4)

    (strlen,) = struct.unpack(">i", data[cur : cur + 4])
    if strlen == -1:
        return None, cur + 4

    cur += 4
    if len(data) < cur + strlen:
        raise _buffer_underflow("long string", data, cur, strlen)

    out = data[cur : cur + strlen]
    return out, cur + strlen


def relative_unpack(fmt, data, cur):
    size = struct.calcsize(fmt)
    if len(data) < cur + size:
        raise _buffer_underflow(fmt, data, cur, size)

    out = struct.unpack(fmt, data[cur : cur + size])
    return out, cur + size


def group_by_topic_and_partition(tuples):
    out = collections.defaultdict(dict)
    for t in tuples:
        out[t.topic][t.partition] = t
    return out
