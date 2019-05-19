# -*- coding: utf-8 -*-
# Copyright 2019 Ciena Corporation
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

import logging
from unittest import TestCase

from twisted.logger import LogLevel, eventAsText
from twisted.python.failure import Failure

from .. import StdlibLogBackend, TwistedLogBackend
from .logtools import capture_logging


class TwistedLogBackendTests(TestCase):
    """
    Test `afkak.TwistedLogBackend`, the :mod:`twisted.logger` backend.

    These tests are pretty simple as they only need to test the plumbing.
    Coverage of rendering is handled by :class:`StdlibLogBackendTests`.

    :ivar log:
        :class:`afkak.Log` instance under test.

    :ivar events: Events emitted by the logger.
    """
    namespace = 'test'

    def setUp(self):
        self.events = []
        self.backend = TwistedLogBackend(self.events.append)
        self.log = self.backend.with_namespace(self.namespace)

    def test_prefix(self):
        """
        The prefixes are prepended to the `log_format` field.
        """
        self.log.debug('foo')
        self.log.with_prefix('foo').debug('bar')
        self.log.with_prefix('foo').with_prefix('bar').debug('biz')

        self.assertEqual([
            'foo',
            'foo bar',
            'foo bar biz',
        ], [e['log_format'] for e in self.events])

    def test_context(self):
        """
        Context is added to the messages emitted.
        """
        self.log.with_context(foo='bar', bar='baz').info('{foo}{bar}', biz='biff')

        [e] = self.events
        self.assertEqual(e['foo'], 'bar')
        self.assertEqual(e['bar'], 'baz')
        self.assertEqual(e['biz'], 'biff')
        self.assertEqual('barbaz', eventAsText(e, False, False, False))


class StdlibLogBackendTests(TestCase):
    """
    Test `afkak.StdlibLogBackend`, the :mod:`logging` library backend.

    These tests use the variable *log* for instances of :class:`afkak.Log`,
    and *logger* for instances of `logging.Logger`.

    :ivar logger:
        :class:`logging.Logger` to use in tests. It is configured not to
        propagate messages upward.

    :ivar log:
        :class:`afkak.Log` attached to *logger*
    """
    namespace = __name__
    logger = logging.getLogger(namespace)
    logger.propagate = False

    def setUp(self):
        self.backend = StdlibLogBackend()
        self.log = self.backend.with_namespace(self.namespace)

    def test_format(self):
        """
        The facade produces `logging.LogRecord` instances that render
        Twisted-style format strings.
        """
        with capture_logging(self.logger) as records:
            self.log.debug('{lvl} {f()}', lvl='debug', f=lambda: 10)
            self.log.info('{lvl} {s!r}', lvl='info', s='20')
            self.log.warn('{lvl} {d[k]}', lvl='warn', d={'k': '30'})
            self.log.error('{lvl} {lst[0]}', lvl='error', lst=[40])
            self.log.with_context(context=50).critical('{lvl} {context}', lvl='critical')

        self.assertEqual(
            ['debug 10', "info '20'", 'warn 30', 'error 40', 'critical 50'],
            [r.getMessage() for r in records],
        )

    def test_prefix(self):
        """
        Each additional prefix is appended to the space-separated prefix list.
        """
        log_with_context = self.log.with_context(prefix1='a', prefix2='b')
        log_with_prefix = log_with_context.with_prefix('[{prefix1}]')
        log_with_second_prefix = log_with_prefix.with_prefix('<{prefix2}>')

        with capture_logging(self.logger) as records:
            self.log.info('hello {who}', who='world')
            log_with_prefix.info('what {noun}', noun='now')
            log_with_second_prefix.info('why?')

        [r1, r2, r3] = records
        self.assertEqual('hello world', r1.getMessage())
        self.assertEqual('[a] what now', r2.getMessage())
        self.assertEqual('[a] <b> why?', r3.getMessage())

    def test_oops(self):
        """
        The failure passed to `oops()` is converted to *exc_info* on the
        `logging.LogRecord`.
        """
        try:
            raise IndentationError('...')
        except Exception:
            f = Failure()
        exc_info = (f.type, f.value, f.getTracebackObject())

        with capture_logging(self.logger) as records:
            self.log.oops(f, 'Frobbed the {frizz}', frizz='froob')
            self.log.oops(f, 'Failed at debug', level=LogLevel.debug)

        [r1, r2] = records
        self.assertEqual('Frobbed the froob', r1.getMessage())
        self.assertEqual('Failed at debug', r2.getMessage())
        self.assertEqual(exc_info, r1.exc_info)
        self.assertEqual(exc_info, r2.exc_info)

    def test_caller(self):
        """
        The caller location information on the `logging.LogRecord` points at
        the actual caller, rather than the facade.
        """
        with capture_logging(self.logger) as records:
            self.log.debug('CALLER LOG MESSAGE')

        # Find the line number of the above line.
        with open(__file__.rstrip('co'), 'rb') as f:
            for i, line in enumerate(f):
                if b"'CALLER LOG MESSAGE'" in line:
                    lineno = i + 1
                    break

        [r] = records
        self.assertEqual(r.funcName, 'test_caller')
        self.assertTrue(r.filename.startswith('test_logronomicon.py'))
        self.assertEqual(r.lineno, lineno)
