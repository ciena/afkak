# -*- coding: utf-8 -*-
# Copyright 2018 Ciena Corporation
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
Tools for testing logging

    with capture_logging(logger) as records:
        logger.info("hello")

    [record] = records
    assert record.msg == "hello"
"""

import logging
import unittest
from contextlib import contextmanager


class _CaptureHandler(logging.Handler):
    """Log hander that captures logging calls

    :ivar list records:
        All of the :class:`logging.LogRecord` objects emitted to this handler.
    """

    def __init__(self):
        super(_CaptureHandler, self).__init__()
        self.records = []

    def emit(self, record):
        """Record a log record"""
        self.records.append(record)


@contextmanager
def capture_logging(logger, level=logging.DEBUG):
    """Context manager that captures log records

    :param logger: :class:`logging.Logger`. Propagation of messages is disabled
        while the context manager is active.
    :param level:
        Log level assigned to the logger while the context manager is active.
        Defaults to `logging.DEBUG`.

        If you pass `logging.NOTSET` the level will be inherited from the
        global configuration, which may vary depending on the test harness in
        use (e.g., nose meddles with it). This may lead to brittle tests.

    :returns: :class:`list` of :class:`logging.LogRecord` objects
    """
    handler = _CaptureHandler()
    was_propagating = logger.propagate
    was_level = logger.level
    logger.propagate = False
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = was_propagating
        logger.setLevel(was_level)


class CaptureLoggingTests(unittest.TestCase):
    """Test :func:`capture_logging()`"""

    logger = logging.getLogger(__name__).getChild("CaptureLoggingTests")

    def test_capture(self):
        """
        Log messages accumulate in the list.
        """
        with capture_logging(self.logger) as records:
            self.logger.info("hello")
            self.logger.debug("world")

        [r1, r2] = records
        assert r1.msg == "hello"
        assert r2.msg == "world"

    def test_propagate(self):
        """
        Propagation is disabled within the context manager and restored after.
        """
        self.logger.propagate = True

        with capture_logging(self.logger):
            self.assertFalse(self.logger.propagate)

        self.assertTrue(self.logger.propagate)

    def test_level(self):
        """
        The logger level is set within the context manager.
        """
        with capture_logging(self.logger, logging.ERROR):
            self.assertEqual(logging.ERROR, self.logger.level)

        self.assertEqual(logging.NOTSET, self.logger.level)
