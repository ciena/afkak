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

"""
Afkak logging infrastructure

This is a facade that can back into :mod:`logging` or :mod:`twisted.logger`.

Features:

- Uses the Twisted model internally, so logs are represented as structured
  event dicts.
- Extends the Twisted model to include *context* data that is automatically
  include in every event.
- Can add a prefix to each message based on the context.
- Doesn't mangle the :class:`logging.LogRecord` *filename*, *funcName*, or
  *lineno* attributes (this is where we must petition elder evils).
"""

import logging

import attr
from twisted.logger import Logger, LogLevel, eventAsText, globalLogPublisher

_logLevelToLoggingLevel = {
    LogLevel.debug: logging.DEBUG,
    LogLevel.info: logging.INFO,
    LogLevel.warn: logging.WARNING,
    LogLevel.error: logging.ERROR,
    LogLevel.critical: logging.CRITICAL,
}


@attr.s(slots=True, str=False)
class _LazyMsg(object):
    """
    A lazily-formatted message for the :mod:`logging` package.

    This is passed to the logging library like::

        logging.debug(_LazyMsg(event))

    It ends up as the :attr:`logging.LogRecord.msg` attribute. If the record's
    :meth:`~logging.LogRecord.getMessage()` method is called (i.e., when
    a handler actually wants to emit the message) the `__str__` method returns
    a formatted message.

    :param dict event:
        :mod:`twisted.logger` style event dict.
    """
    _event = attr.ib()

    def __str__(self):
        return eventAsText(self._event, False, False, False)


# Stub to satisfy linters
def _relay_for(logger):
    pass


# Stub to satisfy linters
class _LogMagic:
    pass


# These methods must be compiled to look like they came from logging.__file__
# because when the logging library walks up the stack to find the call location
# it reports the first call that isn't in the logging/__init__.py source file.
exec(compile(r'''
def _relay_for(logger):
    def relay(event):
        if 'log_failure' in event:
            f = event['log_failure']
            exc_info = (f.type, f.value, f.getTracebackObject())
        else:
            exc_info = None

        level = _logLevelToLoggingLevel[event['log_level']]
        logger.log(level, _LazyMsg(event), exc_info=exc_info)

    return relay


class _LogMagic(object):
    def _emit(self, level, format, kwargs):
        kwargs.update(self._context)
        if self._prefix:
            format = self._prefix + format
        self._logger.emit(level, format, **kwargs)
        event = self._events.pop()
        assert not self._events
        self._observer(event)

    def debug(self, format, **kwargs):
        """
        Emit a log event at debug level

        :param format: Format string `per twisted.logger
            <https://twistedmatrix.com/documents/current/core/howto/logger.html#format-strings>`_
        :type format: str

        :param kwargs: Key/value pairs to include in the event.
        """
        self._emit(LogLevel.debug, format, kwargs)

    def info(self, format, **kwargs):
        """
        See :meth:`.debug()`
        """
        self._emit(LogLevel.info, format, kwargs)

    def warn(self, format, **kwargs):
        """
        See :meth:`.debug()`
        """
        self._emit(LogLevel.warn, format, kwargs)

    def error(self, format, **kwargs):
        """
        See :meth:`.debug()`
        """
        self._emit(LogLevel.error, format, kwargs)

    def critical(self, format, **kwargs):
        """
        See :meth:`.debug()`
        """
        self._emit(LogLevel.critical, format, kwargs)

    def oops(self, failure, format, level=LogLevel.critical, **kwargs):
        """
        Log an unexpected deferred failure

        Use this as a last-gasp errback::

            d = defer.Deferred()
            # ...
            d.addErrback(log.oops, "BUG! {foo}", foo='1')

        The resulting message will include the failure's traceback.

        :param failure: An error
        :type failure: :class:`twisted.python.failure.Failure`

        :param format: Format string `per twisted.logger
            <https://twistedmatrix.com/documents/current/core/howto/logger.html#format-strings>`_
        :type format: str

        :param level:
            Level of the message. By default, :data:`LogLevel.critical` as
            appropriate for a bug.
        :type level: `twisted.logger.LogLevel`

        :returns: `None` to handle the error
        """
        kwargs['log_failure'] = failure
        self._emit(level, format, kwargs)
''', logging._srcfile, 'exec'))


class Log(_LogMagic):
    """
    A logger with Afkak-specific augmentations.

    There is no public constructor. To create an :class:`Log` use one
    of the backend implementations:

      - :class:`StdlibLogBackend.with_namespace()`
      - :class:`TwistedLogBackend.with_namespace()`

    Add context information and prefixes using the :class:`Log` methods:

      - :meth:`with_context()`
      - :meth:`with_prefix()`


    """
    def __init__(self, observer, namespace, context=None, prefix=None):
        """
        This constructor is private.

        :param str namespace:
            Name of the Python module (i.e., ``__name__``). This is used as the
            ``log_namespace`` of the event and should match the Python
            :class:`logging.Logger` that the message is sent to.

        :param observer: :class:`twisted.logger.ILogObserver`

        :param dict context:
            Keys to add to every event dict emitted by this logger.

        :param str prefix:
            A format string (a-la :meth:`str.format()`) that may only reference
            keys in *context*.

        """
        self._observer = observer
        self._namespace = namespace
        self._context = context or {}
        self._prefix = prefix + ' ' if prefix else None
        # FIXME: This should use twisted.logger's extended formatter
        assert prefix is None or prefix.format(**context) is not None  # Doesn't throw.

        # To avoid putting twisted.logger in the call stack (which would
        # confuse logging.Logger.findCaller()) we generate events by invoking
        # _logger which appends them to the _events. This is a gross hack.
        #
        # It would also be possible to reimplement what Logger does, but the
        # "log_logger" key[1] in events is a problem: what if an observer expects
        # that to be an instance of twisted.logger.Logger? We use the real
        # thing out of an abundance of caution.
        #
        # [1]: https://twistedmatrix.com/documents/current/core/howto/logger.html#event-keys-added-by-the-system
        self._events = []
        self._logger = Logger(
            namespace=namespace,
            observer=self._events.append,
        )

    def __repr__(self):
        bits = ['<', self.__class__.__name__, ' ', self._namespace]
        if self._prefix:
            bits.append(' ')
            # FIXME: This should use twisted.logger's extended formatter
            bits.append(self._prefix[:-1].format(**self._context))
        bits.append('>')
        return ''.join(bits)

    def with_context(self, **context):
        """
        Construct a `Log` with additional context.

        :param dict context:
            Additional keys to add to every event emitted by the logger.

        :returns: :class:`afkak.Log` instance
        """
        new_context = self._context.copy()
        new_context.update(context)

        return self.__class__(
            namespace=self._namespace,
            observer=self._observer,
            context=new_context,
            prefix=self._prefix,
        )

    def with_prefix(self, prefix):
        """
        Construct a `Log` with an additional prefix.

        The new instance has the same namespace and context as this instance.
        The new prefix will be added *after* any current prefix, joined by
        a space.

        :param str prefix: Format string `per twisted.logger
            <https://twistedmatrix.com/documents/current/core/howto/logger.html#format-strings>`_

        :returns: :class:`afkak.Log` instance
        """
        if self._prefix and prefix:
            new_prefix = self._prefix + prefix
        elif self._prefix:
            new_prefix = self._prefix
        else:
            new_prefix = prefix

        return self.__class__(
            namespace=self._namespace,
            observer=self._observer,
            context=self._context,
            prefix=new_prefix,
        )


class TwistedLogBackend(object):
    """
    Send Afkak's logs to :mod:`twisted.logger`

    :param observer:
        The log observer to use. By default, the global one.
    """
    def __init__(self, observer=globalLogPublisher):
        self._observer = observer

    def with_namespace(self, namespace):
        return Log(
            observer=self._observer,
            namespace=namespace,
        )


class StdlibLogBackend(object):
    """
    Send Afkak's logs to :mod:`logging`
    """
    def with_namespace(self, namespace):
        return Log(
            observer=_relay_for(logging.getLogger(namespace)),
            namespace=namespace,
        )
