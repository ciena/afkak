# -*- coding: utf-8 -*-
# Copyright (C) 2015 Cyan, Inc.
# Copyright 2018, 2019, 2021 Ciena Corporation
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

import importlib
import os
import struct
import unittest
from unittest.mock import patch

import afkak
from afkak.codec import gzip_decode, gzip_encode, has_gzip, has_snappy, snappy_decode, snappy_encode


class TestCodec(unittest.TestCase):
    @unittest.skipUnless(has_gzip(), "Gzip not available")
    def test_gzip(self):
        for _i in range(100):
            s1 = os.urandom(100)
            s2 = gzip_decode(gzip_encode(s1))
            self.assertEqual(s1, s2)

    @unittest.skipUnless(has_snappy(), "Snappy not available")
    def test_snappy(self):
        for _i in range(100):
            s1 = os.urandom(120)
            s2 = snappy_decode(snappy_encode(s1))
            self.assertEqual(s1, s2)

    @unittest.skipUnless(has_snappy(), "Snappy not available")
    def test_snappy_decode_xerial(self):
        header = b"\x82SNAPPY\x00\x00\x00\x00\x01\x00\x00\x00\x01"
        random_snappy = snappy_encode(b"SNAPPY" * 50)
        block_len = len(random_snappy)
        random_snappy2 = snappy_encode(b"XERIAL" * 50)
        block_len2 = len(random_snappy2)

        to_test = header + struct.pack("!i", block_len) + random_snappy + struct.pack("!i", block_len2) + random_snappy2
        self.assertEqual(snappy_decode(to_test), (b"SNAPPY" * 50) + (b"XERIAL" * 50))

    @unittest.skipUnless(has_snappy(), "Snappy not available")
    def test_snappy_encode_xerial(self):
        to_ensure = (
            b"\x82SNAPPY\x00\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x00\x00\x00\x18"
            b"\xac\x02\x14SNAPPY\xfe\x06\x00\xfe\x06"
            b"\x00\xfe\x06\x00\xfe\x06\x00\x96\x06\x00"
            b"\x00\x00\x00\x18"
            b"\xac\x02\x14XERIAL\xfe\x06\x00\xfe\x06\x00"
            b"\xfe\x06\x00\xfe\x06\x00\x96\x06\x00"
        )

        to_test = (b"SNAPPY" * 50) + (b"XERIAL" * 50)

        compressed = snappy_encode(to_test, xerial_compatible=True, xerial_blocksize=300)
        self.assertEqual(compressed, to_ensure)

    @unittest.skipUnless(has_snappy(), "Snappy not available")
    def test_snappy_raises_when_not_present(self):
        with patch.object(afkak.codec, "has_snappy", return_value=False):
            with self.assertRaises(NotImplementedError):
                snappy_encode(b"Snappy not available")
            with self.assertRaises(NotImplementedError):
                snappy_decode(b"Snappy not available")

    def test_snappy_import_fails(self):
        import sys

        with patch.dict(sys.modules, values={"snappy": None}):
            importlib.reload(afkak.codec)
            self.assertFalse(afkak.codec.has_snappy())
        importlib.reload(afkak.codec)
