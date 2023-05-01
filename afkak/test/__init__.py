# -*- encoding: utf-8 -*-
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

import logging
import os


def _twisted_debug():
    """
    When the ``AFKAK_TWISTED_DEBUG`` environment variable is set, enable
    debugging of deferreds and delayed calls.
    """
    if os.environ.get("AFKAK_TWISTED_DEBUG"):
        from twisted.internet import defer
        from twisted.internet.base import DelayedCall

        defer.setDebugging(True)
        DelayedCall.debug = True


def _nose_log_to_file():
    """
    Nose: still somehow a better test runner than Scalatest.
    """
    path = os.environ.get("AFKAK_TEST_LOG")
    if not path:
        return

    handler = logging.FileHandler(path)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)8s %(threadName)8s" " %(name)10s %(filename)s:%(lineno)d: %(message)s"
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


_nose_log_to_file()
_twisted_debug()
