# -*- coding: utf-8 -*-
# Copyright (C) 2015 Cyan, Inc.

import unittest


class TestPackage(unittest.TestCase):
    def test_top_level_namespace(self):
        import afkak as afkak1

        self.assertEqual(afkak1.KafkaClient.__name__, "KafkaClient")
        self.assertEqual(afkak1.client.__name__, "afkak.client")
        self.assertEqual(afkak1.codec.__name__, "afkak.codec")

    def test_submodule_namespace(self):
        import afkak.client as client1

        self.assertEqual(client1.__name__, "afkak.client")
        self.assertEqual(client1.KafkaClient.__name__, "KafkaClient")

        from afkak import client as client2

        self.assertEqual(client2.__name__, "afkak.client")
        self.assertEqual(client2.KafkaClient.__name__, "KafkaClient")

        from afkak.client import KafkaClient as KafkaClient1

        self.assertEqual(KafkaClient1.__name__, "KafkaClient")

        from afkak.codec import gzip_encode as gzip_encode1

        self.assertEqual(gzip_encode1.__name__, "gzip_encode")

        from afkak import KafkaClient as KafkaClient2

        self.assertEqual(KafkaClient2.__name__, "KafkaClient")

        from afkak.codec import snappy_encode

        self.assertEqual(snappy_encode.__name__, "snappy_encode")
