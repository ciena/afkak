# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
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

"""
Test the afkak.common module.
"""

import unittest

from .. import CODEC_GZIP, CODEC_LZ4, CODEC_SNAPPY, common
from ..common import (
    BrokerResponseError,
    ConsumerMetadataResponse,
    CoordinatorLoadInProgress,
    CoordinatorNotAvailable,
    FetchResponse,
    LeaderNotAvailableError,
    Message,
    MessageSizeTooLargeError,
    NotCoordinator,
    OffsetCommitResponse,
    OffsetFetchResponse,
    OffsetMetadataTooLargeError,
    OffsetOutOfRangeError,
    OffsetResponse,
    ProduceResponse,
    RetriableBrokerResponseError,
    UnknownTopicOrPartitionError,
)


class BrokerResponseErrorTests(unittest.TestCase):
    maxDiff = None

    def test_error_codes(self):
        """
        The `afkak.common.BrokerResponseError.errnos` mapping includes all
        concrete subclasses of `BrokerResponseError` by errno attribute.
        """
        count = 0
        expected = {}
        for name in dir(common):
            value = getattr(common, name)
            if not isinstance(value, type):
                continue
            if value is BrokerResponseError:
                continue
            if value is RetriableBrokerResponseError:
                continue
            if value.__name__ != name:
                continue  # Skip backwards-compatibility aliases.
            if issubclass(value, BrokerResponseError):
                expected[value.errno] = value
                count += 1

        self.assertEqual(expected, BrokerResponseError.errnos)
        self.assertEqual(count, len(BrokerResponseError.errnos), "errno values are reused")

    def test_raise_for_errno_ok(self):
        """
        The errno 0 represents "no error", so
        `BrokerResponseError.raise_for_errno()` doesn't raise an exception.
        """
        BrokerResponseError.raise_for_errno(0)

    def test_raise_for_errno_identity(self):
        """
        `BrokerResponseError.raise_for_errno()` raises an exception based on
        the errnos mapping.
        """
        for code, e in BrokerResponseError.errnos.items():
            self.assertRaises(e, BrokerResponseError.raise_for_errno, code)

    def test_raise_for_responses(self):
        """
        `BrokerResponseError.raise_for_errno()` raises the appropriate
        exception for several response types.
        """
        # response (with .error attribute) -> error type
        responses = [
            (ProduceResponse("topic1", 5, 3, 9), UnknownTopicOrPartitionError),
            (FetchResponse("topic2", 3, 10, 8, []), MessageSizeTooLargeError),
            (OffsetResponse("topic3", 8, 1, []), OffsetOutOfRangeError),
            (OffsetCommitResponse("topic4", 10, 12), OffsetMetadataTooLargeError),
            (OffsetFetchResponse("topic5", 33, 12, "", 5), LeaderNotAvailableError),
            (ConsumerMetadataResponse(15, -1, "", -1), CoordinatorNotAvailable),
            (OffsetFetchResponse("topic6", 23, -1, "", 14), CoordinatorLoadInProgress),
            (OffsetCommitResponse("topic7", 24, 16), NotCoordinator),
        ]

        for resp, e in responses:
            self.assertRaises(e, BrokerResponseError.raise_for_errno, resp.error)

    def test_raise_unknown_errno(self):
        """
        `BrokerResponseError` is raised when there is no subclass of it defined
        for a given errno. This exception indicates that the request cannot be
        retried, as we have no information on it.
        """
        unknown_errno = max(BrokerResponseError.errnos.keys()) + 1

        with self.assertRaises(BrokerResponseError) as cm:
            BrokerResponseError.raise_for_errno(unknown_errno)

        self.assertIs(BrokerResponseError, type(cm.exception))  # Not a subclass.
        self.assertEqual(unknown_errno, cm.exception.errno)
        self.assertFalse(cm.exception.retriable)


class MessageTests(unittest.TestCase):
    def test_repr_keyed(self):
        """
        The key of a message is displayed when present.
        """
        self.assertIn(
            repr(Message(0, CODEC_GZIP, b"key", b"")),
            (
                "<Message v0 CODEC_GZIP key='key' value=''>",  # Python 2
                "<Message v0 CODEC_GZIP key=b'key' value=b''>",  # Python 3
            ),
        )

    def test_repr_unkeyed(self):
        """
        A null message key is elided from the message repr.
        """
        self.assertEqual(
            repr(Message(0, 0, None, None)),
            "<Message v0 value=None>",
        )

    def test_repr_compress(self):
        """
        The type of compression is displayed as a keyword.
        """
        self.assertEqual(
            "<Message v0 CODEC_GZIP value=None>",
            repr(Message(0, CODEC_GZIP, None, None)),
        )
        self.assertEqual(
            "<Message v0 CODEC_SNAPPY value=None>",
            repr(Message(0, CODEC_SNAPPY, None, None)),
        )
        self.assertEqual("<Message v0 CODEC_LZ4 value=None>", repr(Message(0, CODEC_LZ4, None, None)))

    def test_repr_long_value(self):
        """
        The value of the message is truncated if it exceeds 1024 bytes.
        """
        value = b"a" * 512 + b"b" * 1024
        self.assertEqual(
            "<Message v0 value=1,536 bytes {!r}...>".format(b"a" * 512),
            repr(Message(0, 0, None, value)),
        )
