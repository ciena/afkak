# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2016, 2017, 2018, 2019 Ciena Corporation
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
import random
import string

from ..common import SendRequest
from twisted.internet import task
from twisted.internet.defer import Deferred

log = logging.getLogger(__name__)

__all__ = [
    "async_delay",
    "first",
    "make_send_requests",
    "random_string",
]


def first(deferreds):
    """Get the first result. Cancel the rest.

    :param deferreds:
        A sequence of `twisted.internet.defer.Deferred` instances.

        Passing a deferred to *first* transfers ownership: the caller must not
        add callbacks or cancel it. *first* cancels all other deferreds as soon
        as one fires.

        This sequence must not be mutated by the caller. *first* does not
        defensively copy.

    :returns: `Deferred` that fires with the result of the first deferred to
        fire or fail. Canceling this deferred cancels all of the deferreds.
    """

    def cancel_all(self):
        for d in deferreds:
            d.cancel()

    result_d = Deferred(cancel_all)

    def one_result(result, source):
        if result_d.called:
            return
        result_d.callback(result)
        for d in deferreds:
            if d is not source:
                d.cancel()

    for d in deferreds:
        d.addBoth(one_result, d)
    return result_d


def random_string(length):
    # Random.choice can be very slow for large amounts of data, so 'cheat'
    if length <= 50:
        s = "".join(random.choice(string.ascii_letters) for _i in range(length))
    else:
        r = random_string(50)
        s = "".join(r for i in range(length // 50))
        if length % 50:
            s += r[0 : (length % 50)]
    assert len(s) == length
    return s


def async_delay(timeout=0.01, clock=None):
    if clock is None:
        from twisted.internet import reactor as clock

    return task.deferLater(clock, timeout, lambda: None)


def make_send_requests(msgs, topic=None, key=None):
    return [SendRequest(topic, key, msgs, None)]
