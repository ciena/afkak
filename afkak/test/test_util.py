# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2017, 2018 Ciena Corporation.

import struct
import unittest

import afkak.common
from afkak import _util as util
from afkak.common import BufferUnderflowError


class TestUtil(unittest.TestCase):
    def test_write_int_string(self):
        self.assertEqual(
            util.write_int_string(b"some string"),
            b"\x00\x00\x00\x0bsome string",
        )

    def test_write_int_string__empty(self):
        self.assertEqual(
            util.write_int_string(b""),
            b"\x00\x00\x00\x00",
        )

    def test_write_int_string__null(self):
        self.assertEqual(
            util.write_int_string(None),
            b"\xff\xff\xff\xff",
        )

    def test_read_int_string(self):
        self.assertEqual(util.read_int_string(b"\xff\xff\xff\xff", 0), (None, 4))
        self.assertEqual(util.read_int_string(b"\x00\x00\x00\x00", 0), (b"", 4))
        self.assertEqual(
            util.read_int_string(b"\x00\x00\x00\x0bsome string", 0),
            (b"some string", 15),
        )

    def test_read_int_string_missing_length(self):
        """
        BufferUnderflowError results when there aren't enough bytes for
        a string length at the expected position.
        """
        with self.assertRaises(BufferUnderflowError) as context:
            util.read_int_string(b"\x01", 0)

        self.assertEqual(
            "Not enough data to read long string length at offset 0: 4 bytes required, but 1 available.",
            str(context.exception),
        )

    def test_read_int_string_insufficient_bytes(self):
        """
        BufferUnderflowError results when there aren't enough bytes left to
        satisfy the string length prefix.
        """
        with self.assertRaises(BufferUnderflowError) as context:
            util.read_int_string(b"pad\x00\x00\x00\xffstr", 3)

        self.assertEqual(
            "Not enough data to read long string at offset 7: 255 bytes required, but 3 available.",
            str(context.exception),
        )

    def test_read_int_string__insufficient_data_1(self):
        with self.assertRaises(afkak.common.BufferUnderflowError):
            util.read_int_string(b"\x00\x00\x00\x021", 0)

    def test_read_int_string__insufficient_data_2(self):
        with self.assertRaises(afkak.common.BufferUnderflowError):
            util.read_int_string(b"\x00\x00\x00\x021", 2)

    def test_write_short_bytes(self):
        self.assertEqual(
            util.write_short_bytes(b"some string"),
            b"\x00\x0bsome string",
        )

    def test_write_short_bytes__empty(self):
        self.assertEqual(
            util.write_short_bytes(b""),
            b"\x00\x00",
        )

    def test_write_short_bytes__null(self):
        self.assertEqual(
            util.write_short_bytes(None),
            b"\xff\xff",
        )

    def test_write_short_bytes__too_long(self):
        with self.assertRaises(struct.error):
            util.write_short_bytes(b" " * 33000)

    def test_write_short_ascii(self):
        """
        `write_short_ascii()` accepts a `str` where all characters are in the
        ASCII range.
        """
        self.assertEqual(util.write_short_ascii("ascii"), b"\x00\x05ascii")

    def test_write_short_ascii_not_ascii(self):
        """
        `write_short_ascii()` raises `UnicodeEncodeError` when given characters
        outside the ASCII range.
        """
        with self.assertRaises(UnicodeEncodeError):
            util.write_short_ascii("\N{SNOWMAN}")

    def test_write_short_ascii_nonstr(self):
        """
        `write_short_ascii()` raises `TypeError` when passed an argument that
        isn't a `str`.
        """
        self.assertRaises(TypeError, util.write_short_ascii, 1)
        self.assertRaises(TypeError, util.write_short_ascii, b"ascii")

    def test_write_short_ascii_none(self):
        """
        `write_short_ascii()` accepts `None`, which is represented as a special
        null sequence.
        """
        self.assertEqual(util.write_short_ascii(None), b"\xff\xff")

    def test_write_short_text(self):
        """
        `write_short_text()` encodes the input string as UTF-8.
        """
        self.assertEqual(
            util.write_short_text("\N{SNOWMAN}"),
            b"\x00\x03\xe2\x98\x83",
        )

    def test_write_short_text_nonstr(self):
        """
        `write_short_ascii()` raises `TypeError` when passed an argument that
        isn't a `str`.
        """
        self.assertRaises(TypeError, util.write_short_ascii, 1)
        self.assertRaises(TypeError, util.write_short_ascii, b"bytes")

    def test_write_short_text_none(self):
        """
        `write_short_text()` accepts `None`, which is represented as a special
        null sequence.
        """
        self.assertEqual(util.write_short_text(None), b"\xff\xff")

    def test_read_short_bytes(self):
        self.assertEqual(util.read_short_bytes(b"\xff\xff", 0), (None, 2))
        self.assertEqual(util.read_short_bytes(b"\x00\x00", 0), (b"", 2))
        self.assertEqual(
            util.read_short_bytes(b"\x00\x0bsome string", 0),
            (b"some string", 13),
        )

    def test_read_short_bytes__insufficient_data_1(self):
        with self.assertRaises(afkak.common.BufferUnderflowError):
            util.read_short_bytes(b"\x00\x021", 2)

    def test_read_short_bytes__insufficient_data_2(self):
        with self.assertRaises(afkak.common.BufferUnderflowError):
            util.read_short_bytes(b"\x00\x021", 0)

    def test_relative_unpack(self):
        self.assertEqual(
            util.relative_unpack(">hh", b"\x00\x01\x00\x00\x02", 0),
            ((1, 0), 4),
        )

    def test_relative_unpack__insufficient_data(self):
        with self.assertRaises(afkak.common.BufferUnderflowError):
            util.relative_unpack(">hh", b"\x00", 0)

    def test_group_by_topic_and_partition(self):
        t = afkak.common.TopicAndPartition

        lst = [
            t("a", 1),
            t("a", 1),
            t("a", 2),
            t("a", 3),
            t("b", 3),
        ]

        self.assertEqual(
            util.group_by_topic_and_partition(lst),
            {
                "a": {
                    1: t("a", 1),
                    2: t("a", 2),
                    3: t("a", 3),
                },
                "b": {
                    3: t("b", 3),
                },
            },
        )
