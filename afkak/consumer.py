# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2016, 2017, 2018 Ciena Corporation
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
import sys
from numbers import Integral

from afkak._util import _coerce_consumer_group, _coerce_topic
from afkak.common import (
    OFFSET_COMMITTED,
    OFFSET_EARLIEST,
    OFFSET_LATEST,
    OFFSET_NOT_COMMITTED,
    TIMESTAMP_INVALID,
    ConsumerFetchSizeTooSmall,
    FetchRequest,
    IllegalGeneration,
    InvalidConsumerGroupError,
    InvalidGroupId,
    KafkaError,
    OffsetCommitRequest,
    OffsetFetchRequest,
    OffsetOutOfRangeError,
    OffsetRequest,
    OperationInProgress,
    RestartError,
    RestopError,
    SourcedMessage,
    UnknownMemberId,
)

from twisted.internet.defer import CancelledError, Deferred, fail, inlineCallbacks, maybeDeferred, succeed
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

REQUEST_RETRY_MIN_DELAY = 0.1  # Initial wait to retry a request after failure
REQUEST_RETRY_MAX_DELAY = 30.0  # When retrying a request, max delay (seconds)
REQUEST_RETRY_FACTOR = 1.20205  # Factor by which we increase our delay

FETCH_MIN_BYTES = 64 * 1024  # server waits for min. 64K bytes of messages
FETCH_MAX_WAIT_TIME = 100  # server waits 100 millisecs for messages
FETCH_BUFFER_SIZE_BYTES = 128 * 1024  # Our initial fetch buffer size

# How often we auto-commit (msgs, millisecs)
AUTO_COMMIT_MSG_COUNT = 100
AUTO_COMMIT_INTERVAL = 5000


class Consumer(object):
    """A simple Kafka consumer implementation

    This consumer consumes a single partition from a single topic, optionally
    automatically committing offsets.  Use it as follows:

    - Create an instance of :class:`afkak.KafkaClient` with cluster
      connectivity details.
    - Create the :class:`Consumer`, supplying the client, topic, partition,
      processor function, and optionally fetch specifics, a consumer group,
      and a commit policy.
    - Call :meth:`.start` with the offset within the partition at which to
      start consuming messages. See :meth:`.start` for details.
    - Process the messages in your :attr:`.processor` callback, returning a
      :class:`~twisted.internet.defer.Deferred` to provide backpressure as
      needed.
    - Once processing resolves, :attr:`.processor` will be called again with
      the next batch of messages.
    - When desired, call :meth:`.shutdown` on the :class:`Consumer` to halt
      calls to the :attr:`processor` function and commit progress (if
      a *consumer_group* is specified).

    A :class:`Consumer` may be restarted once stopped.

    :ivar client:
        Connected :class:`KafkaClient` for submitting requests to the Kafka
        cluster.
    :ivar str topic:
        The topic from which to consume messages.
    :ivar int partition:
        The partition from which to consume.
    :ivar callable processor:
        The callback function to which the consumer and lists of messages
        (:class:`afkak.common.SourcedMessage`) will be submitted
        for processing.  The function may return
        a :class:`~twisted.internet.defer.Deferred` and will not be called
        again until this Deferred resolves.
    :ivar str consumer_group:
        Optional consumer group ID for committing offsets of processed
        messages back to Kafka.
    :ivar bytes commit_metadata:
        Optional metadata to store with offsets commit.
    :ivar int auto_commit_every_n:
        Number of messages after which the consumer will automatically
        commit the offset of the last processed message to Kafka. Zero
        disables, defaulted to :data:`AUTO_COMMIT_MSG_COUNT`.
    :ivar int auto_commit_every_ms:
        Time interval in milliseconds after which the consumer will
        automatically commit the offset of the last processed message to
        Kafka. Zero disables, defaulted to :data:`AUTO_COMMIT_INTERVAL`.
    :ivar int fetch_size_bytes:
        Number of bytes to request in a :class:`FetchRequest`.  Kafka will
        defer fulfilling the request until at least this many bytes can be
        returned.
    :ivar int fetch_max_wait_time:
        Max number of milliseconds the server should wait for that many
        bytes.
    :ivar int buffer_size:
        default 128K. Initial number of bytes to tell Kafka we have
        available. This will be raised x16 up to 1MB then double up to...
    :ivar int max_buffer_size:
        Max number of bytes to tell Kafka we have available.  `None` means
        no limit (the default). Must be larger than the largest message we
        will find in our topic/partitions.
    :ivar float request_retry_init_delay:
        Number of seconds to wait before retrying a failed request to
        Kafka.
    :ivar float request_retry_max_delay:
        Maximum number of seconds to wait before retrying a failed request
        to Kafka (the delay is increased on each failure and reset to the
        initial delay upon success).
    :ivar int request_retry_max_attempts:
        Maximum number of attempts to make for any request. Default of zero
        means retry forever; other values must be positive and indicate
        the number of attempts to make before returning failure.


    :ivar int auto_offset_reset:
        What action should be taken when the broker responds to a fetch request
        with `OffsetOutOfRangeError`?

        - `OFFSET_EARLIEST`: request the oldest available messages. The
          consumer will read every message in the topic.
        - `OFFSET_LATEST`: request the most recent messages (this is the Java
          consumer's default). The consumer will read messages once new
          messages are produced to the topic.
        - `None`: fail on `OffsetOutOfRangeError` (Afkak's default). The
          `Deferred` returned by :meth:`Consumer.start()` will errback. The
          caller may call :meth:`~.start()` again with the desired offset.

        The broker returns `OffsetOutOfRangeError` when the client requests an
        offset that isn't valid. This may mean that the requested offset no
        longer exists, e.g. if it was removed due to age.
    """

    def __init__(
        self,
        client,
        topic,
        partition,
        processor,
        consumer_group=None,
        commit_metadata=None,
        auto_commit_every_n=None,
        auto_commit_every_ms=None,
        fetch_size_bytes=FETCH_MIN_BYTES,
        fetch_max_wait_time=FETCH_MAX_WAIT_TIME,
        buffer_size=FETCH_BUFFER_SIZE_BYTES,
        max_buffer_size=None,
        request_retry_init_delay=REQUEST_RETRY_MIN_DELAY,
        request_retry_max_delay=REQUEST_RETRY_MAX_DELAY,
        request_retry_max_attempts=0,
        auto_offset_reset=None,
        commit_consumer_id="",
        commit_generation_id=-1,
    ):
        # Store away parameters
        self.client = client  # KafkaClient
        self.topic = topic = _coerce_topic(topic)
        self.partition = partition  # The partition within the topic we consume
        self.processor = processor  # The callback we call with the msg list
        # Commit related parameters (Ensure the attr. exist, even if None)
        if consumer_group is not None:
            consumer_group = _coerce_consumer_group(consumer_group)
        self.consumer_group = consumer_group
        self.commit_metadata = commit_metadata
        if commit_metadata is not None and not isinstance(commit_metadata, bytes):
            raise TypeError("commit_metadata={!r} should be bytes".format(commit_metadata))
        # commit related parameters when using a coordinated consumer group
        self.commit_consumer_id = commit_consumer_id
        self.commit_generation_id = commit_generation_id
        self.auto_commit_every_n = None
        self.auto_commit_every_s = None
        if consumer_group:
            # Auto committing is possible...
            if auto_commit_every_n is None:
                auto_commit_every_n = AUTO_COMMIT_MSG_COUNT
            if auto_commit_every_ms is None:
                auto_commit_every_ms = AUTO_COMMIT_INTERVAL
            if not isinstance(auto_commit_every_n, Integral):
                raise ValueError("auto_commit_every_n parameter must be " "subtype of Integral")
            if not isinstance(auto_commit_every_ms, Integral):
                raise ValueError("auto_commit_every_ms parameter must be " "subtype of Integral")
            if auto_commit_every_ms < 0 or auto_commit_every_n < 0:
                raise ValueError("auto_commit_every_ms and auto_commit_every_n" " must be non-negative")
            self.auto_commit_every_n = auto_commit_every_n
            self.auto_commit_every_s = float(auto_commit_every_ms) / 1000
        else:
            if auto_commit_every_ms or auto_commit_every_n:
                raise ValueError("An auto_commit_every_x argument set without " "specifying consumer_group")
        # Fetch related instance variables
        self.fetch_min_bytes = fetch_size_bytes
        self.fetch_max_wait_time = int(fetch_max_wait_time)
        self.buffer_size = buffer_size
        self.max_buffer_size = max_buffer_size
        # request retry timing
        self.retry_delay = float(request_retry_init_delay)  # fetch only
        self.retry_init_delay = float(request_retry_init_delay)
        self.retry_max_delay = float(request_retry_max_delay)
        self.request_retry_max_attempts = int(request_retry_max_attempts)
        if not isinstance(request_retry_max_attempts, Integral) or request_retry_max_attempts < 0:
            raise ValueError("request_retry_max_attempts must be non-negative integer")
        self._fetch_attempt_count = 1
        if auto_offset_reset not in [None, OFFSET_EARLIEST, OFFSET_LATEST]:
            raise ValueError("auto_offset_reset must be in 'None', 'OFFSET_EARLIEST', 'OFFSET_LATEST'")
        self.auto_offset_reset = auto_offset_reset
        # # Internal state tracking attributes
        self._fetch_offset = None  # We don't know at what offset to fetch yet
        self._last_processed_offset = None  # Last msg processed offset
        self._last_committed_offset = None  # The last offset stored in Kafka
        self._stopping = False  # We're not stopping yet...
        self._shuttingdown = False  # We're not shutting down either
        self._shutdown_d = None  # deferred for tracking shutdown request
        self._commit_looper = None  # Looping call for auto-commit
        self._commit_looper_d = None  # Deferred for running looping call
        self._commit_ds = []  # Deferreds to notify when commit completes
        self._commit_req = None  # Track outstanding commit request
        # For tracking various async operations
        self._start_d = None  # deferred for alerting user of errors
        self._request_d = None  # outstanding KafkaClient request deferred
        self._retry_call = None  # IDelayedCall object for delayed retries
        self._commit_call = None  # IDelayedCall for delayed commit retries
        self._msg_block_d = None  # deferred for each block of messages
        self._processor_d = None  # deferred for a result from processor
        self._state = "initialized"  # Keep track of state for debugging
        # Check parameters for sanity
        if max_buffer_size is not None and buffer_size > max_buffer_size:
            raise ValueError(
                "buffer_size (%d) is greater than " "max_buffer_size (%d)" % (buffer_size, max_buffer_size)
            )
        if not isinstance(self.partition, Integral):
            raise ValueError("partition parameter must be subtype of Integral")

    def __repr__(self):
        return "<{} {}/{} {}>".format(
            self.__class__.__name__,
            self.topic,
            self.partition,
            self._state,
        )
        # TODO Add commit_consumer_id if applicable

    @property
    def last_processed_offset(self):
        """
        Offset of the last message that was successfully processed, or `None`
        if no message has been processed yet (read-only). This is updated only
        once the processor function returns and any deferred it returns
        succeeds.

        :rtype: Optional[int]
        """
        return self._last_processed_offset

    @property
    def last_committed_offset(self):
        """
        The last offset that was successfully commited to Kafka, or `None` if
        no offset has been committed yet (read-only).

        :rtype: Optional[int]
        """
        return self._last_committed_offset

    def start(self, start_offset):
        """
        Starts fetching messages from Kafka and delivering them to the
        :attr:`.processor` function.

        :param int start_offset:
            The offset within the partition from which to start fetching.
            Special values include: :const:`OFFSET_EARLIEST`,
            :const:`OFFSET_LATEST`, and :const:`OFFSET_COMMITTED`. If the
            supplied offset is :const:`OFFSET_EARLIEST` or
            :const:`OFFSET_LATEST` the :class:`Consumer` will use the
            OffsetRequest Kafka API to retrieve the actual offset used for
            fetching. In the case :const:`OFFSET_COMMITTED` is used,
            `commit_policy` MUST be set on the Consumer, and the Consumer
            will use the OffsetFetchRequest Kafka API to retrieve the actual
            offset used for fetching.

        :returns:
            :class:`~twisted.internet.defer.Deferred` that will fire when the
            consumer is stopped:

            * It will succeed with the value of
              :attr:`last_processed_offset`, or
            * Fail when the :class:`Consumer` encounters an error from which
              it is unable to recover, such as an exception thrown by the
              processor or an unretriable broker error.

        :raises: :exc:`RestartError` if already running.
        """
        # Have we been started already, and not stopped?
        if self._start_d is not None:
            raise RestartError("Start called on already-started consumer")

        # Keep track of state for debugging
        self._state = "started"

        # Create and return a deferred for alerting on errors/stoppage
        start_d = self._start_d = Deferred()

        # Start a new fetch request, possibly just for the starting offset
        self._fetch_offset = start_offset
        self._do_fetch()

        # Set up the auto-commit timer, if needed
        if self.consumer_group and self.auto_commit_every_s:
            self._commit_looper = LoopingCall(self._auto_commit)
            self._commit_looper.clock = self.client.reactor
            self._commit_looper_d = self._commit_looper.start(self.auto_commit_every_s, now=False)
            self._commit_looper_d.addCallbacks(self._commit_timer_stopped, self._commit_timer_failed)
        return start_d

    def shutdown(self):
        """Gracefully shutdown the consumer

        Consumer will complete any outstanding processing, commit its current
        offsets (if so configured) and stop.

        :returns: :class:`Deferred` that fires with the value of
            :attr:`last_processed_offset`. It may fail if a commit fails or
            with :exc:`RestopError` if the consumer is not running.
        """

        def _handle_shutdown_commit_success(result):
            """Handle the result of the commit attempted by shutdown"""
            self._shutdown_d, d = None, self._shutdown_d
            self.stop()
            self._shuttingdown = False  # Shutdown complete
            d.callback(self._last_processed_offset)

        def _handle_shutdown_commit_failure(failure):
            """Handle failure of commit() attempted by shutdown"""
            if failure.check(OperationInProgress):
                failure.value.deferred.addCallback(_commit_and_stop)
                return

            self._shutdown_d, d = None, self._shutdown_d
            self.stop()
            self._shuttingdown = False  # Shutdown complete
            d.errback(failure)

        def _commit_and_stop(result):
            """Commit the current offsets (if needed) and stop the consumer"""
            if not self.consumer_group:  # No consumer group, no committing
                return _handle_shutdown_commit_success(None)

            # Need to commit prior to stopping
            self.commit().addCallbacks(_handle_shutdown_commit_success, _handle_shutdown_commit_failure)

        # If we're not running, return an failure
        if self._start_d is None:
            return fail(Failure(RestopError("Shutdown called on non-running consumer")))
        # If we're called multiple times, return a failure
        if self._shutdown_d:
            return fail(Failure(RestopError("Shutdown called more than once.")))
        # Set our _shuttingdown flag, so our _process_message routine will stop
        # feeding new messages to the processor, and fetches won't be retried
        self._shuttingdown = True
        # Keep track of state for debugging
        self._state = "shutting down"
        # TODO: This was added as part of coordinated consumer support,
        # but it belongs in the constructor if it is even necessary.
        # don't let commit requests retry forever and prevent shutdown
        if not self.request_retry_max_attempts:
            self.request_retry_max_attempts = 2

        # Create a deferred to track the shutdown
        self._shutdown_d = d = Deferred()

        # Are we waiting for the processor to complete? If so, when it's done,
        # commit our offsets and stop.
        if self._processor_d:
            self._processor_d.addCallback(_commit_and_stop)
        else:
            # No need to wait for the processor, we can commit and stop now
            _commit_and_stop(None)

        # return the deferred
        return d

    def stop(self):
        """
        Stop the consumer and return offset of last processed message.  This
        cancels all outstanding operations.  Also, if the deferred returned
        by `start` hasn't been called, it is called with the value of
        :attr:`last_processed_offset`.

        :raises: :exc:`RestopError` if the :class:`Consumer` is not running.
        """
        if self._start_d is None:
            raise RestopError("Stop called on non-running consumer")

        self._stopping = True
        # Keep track of state for debugging
        self._state = "stopping"
        # Are we waiting for a request to come back?
        if self._request_d:
            self._request_d.cancel()
        # Are we working our way through a block of messages?
        if self._msg_block_d:
            # Need to add a cancel handler...
            _msg_block_d, self._msg_block_d = self._msg_block_d, None
            _msg_block_d.addErrback(lambda fail: fail.trap(CancelledError))
            _msg_block_d.cancel()
        # Are we waiting for the processor to complete?
        if self._processor_d:
            self._processor_d.cancel()
        # Are we waiting to retry a request?
        if self._retry_call:
            self._retry_call.cancel()
        # Are we waiting on a commit request?
        if self._commit_ds:
            while self._commit_ds:
                d = self._commit_ds.pop()
                d.cancel()
        if self._commit_req:
            self._commit_req.cancel()
        # Are we waiting to retry a commit?
        if self._commit_call:
            self._commit_call.cancel()
        # Do we have an auto-commit looping call?
        if self._commit_looper is not None:
            self._commit_looper.stop()
        # Done stopping
        self._stopping = False
        # Keep track of state for debugging
        self._state = "stopped"

        # Clear and possibly callback our start() Deferred
        self._start_d, d = None, self._start_d
        if not d.called:
            d.callback(self._last_processed_offset)

        # Return the offset of the message we last processed.
        return self._last_processed_offset

    def commit(self):
        """
        Commit the last processed offset

        Immediately commit the value of :attr:`last_processed_offset` if it
        differs from :attr:`last_committed_offset`.

        .. note::

            It is possible to commit a smaller offset than Kafka has stored.
            This is by design, so we can reprocess a Kafka message stream if
            desired.

        On error, will retry according to :attr:`request_retry_max_attempts`
        (by default, forever).

        If called while a commit operation is in progress, and new messages
        have been processed since the last request was sent then the commit
        will fail with :exc:`OperationInProgress`.  The
        :exc:`OperationInProgress` exception wraps
        a :class:`~twisted.internet.defer.Deferred` which fires when the
        outstanding commit operation completes.

        :returns:
            A :class:`~twisted.internet.defer.Deferred` which resolves with the
            committed offset when the operation has completed.  It will resolve
            immediately if the current offset and the last committed offset do
            not differ.
        """
        # Can't commit without a consumer_group
        if not self.consumer_group:
            return fail(Failure(InvalidConsumerGroupError("Bad Group_id:{0!r}".format(self.consumer_group))))
        # short circuit if we are 'up to date', or haven't processed anything
        if (self._last_processed_offset is None) or (self._last_processed_offset == self._last_committed_offset):
            return succeed(self._last_committed_offset)

        # If we're currently processing a commit we return a failure
        # with a deferred we'll fire when the in-progress one completes
        if self._commit_ds:
            d = Deferred()
            self._commit_ds.append(d)
            return fail(OperationInProgress(d))

        # Ok, we have processed messages since our last commit attempt, and
        # we're not currently waiting on a commit request to complete:
        # Start a new one
        d = Deferred()
        self._commit_ds.append(d)

        # Send the request
        self._send_commit_request()

        # Reset the commit_looper here, rather than on success to give
        # more stability to the commit interval.
        if self._commit_looper is not None:
            self._commit_looper.reset()

        # return the deferred
        return d

    # # Private Methods # #

    def _retry_auto_commit(self, result, by_count=False):
        self._auto_commit(by_count)
        return result

    def _auto_commit(self, by_count=False):
        """Check if we should start a new commit operation and commit"""
        # Check if we are even supposed to do any auto-committing
        if (
            self._stopping
            or self._shuttingdown
            or (not self._start_d)
            or (self._last_processed_offset is None)
            or (not self.consumer_group)
            or (by_count and not self.auto_commit_every_n)
        ):
            return

        # If we're auto_committing because the timer expired, or by count and
        # we don't have a record of our last_committed_offset, or we've
        # processed enough messages since our last commit, then try to commit
        if (
            not by_count
            or self._last_committed_offset is None
            or (self._last_processed_offset - self._last_committed_offset) >= self.auto_commit_every_n
        ):
            if not self._commit_ds:
                commit_d = self.commit()
                commit_d.addErrback(self._handle_auto_commit_error)
            else:
                # We're waiting on the last commit to complete, so add a
                # callback to be called when the current request completes
                d = Deferred()
                d.addCallback(self._retry_auto_commit, by_count)
                self._commit_ds.append(d)

    def _retry_fetch(self, after=None):
        """
        Schedule a delayed :meth:`_do_fetch` call after a failure

        :param float after:
            The delay in seconds after which to do the retried fetch. If
            `None`, our internal :attr:`retry_delay` is used, and adjusted by
            :const:`REQUEST_RETRY_FACTOR`.
        """

        # Have we been told to stop or shutdown?  Then don't actually retry.
        if self._stopping or self._shuttingdown or self._start_d is None:
            # Stopping, or stopped already? No more fetching.
            return
        if self._retry_call is None:
            if after is None:
                after = self.retry_delay
                self.retry_delay = min(self.retry_delay * REQUEST_RETRY_FACTOR, self.retry_max_delay)

            self._fetch_attempt_count += 1
            self._retry_call = self.client.reactor.callLater(after, self._do_fetch)

    def _handle_offset_response(self, responses):
        """
        Handle responses to both OffsetRequest and OffsetFetchRequest, since
        they are similar enough.

        :param responses:
            A tuple of a single OffsetFetchResponse or OffsetResponse
        """
        # Got a response, clear our outstanding request deferred
        self._request_d = None

        # Successful request, reset our retry delay, count, etc
        self.retry_delay = self.retry_init_delay
        self._fetch_attempt_count = 1

        [response] = responses
        if hasattr(response, "offsets"):
            # It's a response to an OffsetRequest
            self._fetch_offset = response.offsets[0]
        else:
            # It's a response to an OffsetFetchRequest
            # Make sure we got a valid offset back. Kafka uses -1 to indicate
            # no committed offset was retrieved
            if response.offset == OFFSET_NOT_COMMITTED:
                if self.auto_offset_reset == OFFSET_LATEST:
                    self._fetch_offset = OFFSET_LATEST
                else:
                    self._fetch_offset = OFFSET_EARLIEST
            else:
                self._fetch_offset = response.offset + 1
                self._last_committed_offset = response.offset
        self._do_fetch()

    def _handle_offset_error(self, failure):
        """
        Retry the offset fetch request if appropriate.

        Once the :attr:`.retry_delay` reaches our :attr:`.retry_max_delay`, we
        log a warning.  This should perhaps be extended to abort sooner on
        certain errors.
        """
        # outstanding request got errback'd, clear it
        self._request_d = None

        if self._stopping and failure.check(CancelledError):
            # Not really an error
            return
        # Do we need to abort?
        if self.request_retry_max_attempts != 0 and self._fetch_attempt_count >= self.request_retry_max_attempts:
            log.debug(
                "%r: Exhausted attempts: %d fetching offset from kafka",
                self,
                self.request_retry_max_attempts,
                exc_info=(failure.type, failure.value, failure.getTracebackObject()),
            )
            self._start_d.errback(failure)
            return
        # Decide how to log this failure... If we have retried so many times
        # we're at the retry_max_delay, then we log at warning every other time
        # debug otherwise
        if self.retry_delay < self.retry_max_delay or 0 == (self._fetch_attempt_count % 2):
            log.debug("%r: Failure fetching offset from kafka: %r", self, failure)
        else:
            # We've retried until we hit the max delay, log at warn
            log.warning("%r: Still failing fetching offset from kafka: %r", self, failure)
        self._retry_fetch()

    def _clear_processor_deferred(self, result):
        self._processor_d = None  # It has fired, we can clear it
        return result

    def _update_processed_offset(self, result, offset):
        log.debug("%s: processor returned %r at offset %d", self, result, offset)
        self._last_processed_offset = offset
        self._auto_commit(by_count=True)

    def _clear_commit_req(self, result):
        self._commit_req = None  # It has fired, we can clear it
        return result

    def _update_committed_offset(self, result, offset):
        # successful commit request completed
        self._last_committed_offset = offset
        self._deliver_commit_result(offset)
        return offset

    def _deliver_commit_result(self, result):
        # Let anyone waiting know the commit completed. Handle the case where
        # they try to commit from the callback by preserving self._commit_ds
        # as a local, but clearing the attribute itself.
        commit_ds, self._commit_ds = self._commit_ds, []
        while commit_ds:
            d = commit_ds.pop()
            d.callback(result)

    def _send_commit_request(self, retry_delay=None, attempt=None):
        """Send a commit request with our last_processed_offset"""
        # If there's a _commit_call, and it's not active, clear it, it probably
        # just called us...
        if self._commit_call and not self._commit_call.active():
            self._commit_call = None

        # Make sure we only have one outstanding commit request at a time
        if self._commit_req is not None:
            raise OperationInProgress(self._commit_req)

        # Handle defaults
        if retry_delay is None:
            retry_delay = self.retry_init_delay
        if attempt is None:
            attempt = 1

        # Create new OffsetCommitRequest with the latest processed offset
        commit_offset = self._last_processed_offset
        commit_request = OffsetCommitRequest(
            self.topic,
            self.partition,
            commit_offset,
            TIMESTAMP_INVALID,
            self.commit_metadata,
        )
        log.debug(
            "Committing off=%s grp=%s tpc=%s part=%s req=%r",
            self._last_processed_offset,
            self.consumer_group,
            self.topic,
            self.partition,
            commit_request,
        )

        # Send the request, add our callbacks
        self._commit_req = d = self.client.send_offset_commit_request(
            self.consumer_group,
            [commit_request],
            group_generation_id=self.commit_generation_id,
            consumer_id=self.commit_consumer_id,
        )

        d.addBoth(self._clear_commit_req)
        d.addCallbacks(
            callback=self._update_committed_offset,
            callbackArgs=(commit_offset,),
            errback=self._handle_commit_error,
            errbackArgs=(commit_offset, retry_delay, attempt),
        )

    def _handle_commit_error(self, failure, commit_offset, retry_delay, attempt):
        """Retry the commit request, depending on failure type

        Depending on the type of the failure, we retry the commit request
        with the latest processed offset, or callback/errback self._commit_ds
        """
        # Check if we are stopping and the request was cancelled
        if self._stopping and failure.check(CancelledError):
            # Not really an error
            return self._deliver_commit_result(self._last_committed_offset)

        # Check that the failure type is a Kafka error...this could maybe be
        # a tighter check to determine whether a retry will succeed...
        if not failure.check(KafkaError):
            log.error(
                "Unhandleable failure during commit attempt: %r\n\t%r",
                failure,
                failure.getBriefTraceback(),
            )
            return self._deliver_commit_result(failure)

        # the server may reject our commit because we have lost sync with the group
        if failure.check(IllegalGeneration, InvalidGroupId, UnknownMemberId):
            log.error(
                "Unretriable failure during commit attempt: %r\n\t%r",
                failure,
                failure.getBriefTraceback(),
            )
            # we need to notify the coordinator here
            self._deliver_commit_result(failure)
            return

        # Do we need to abort?
        if self.request_retry_max_attempts != 0 and attempt >= self.request_retry_max_attempts:
            log.debug(
                "%r: Failed to commit offset %s %d times: out of retries",
                self,
                commit_offset,
                self.request_retry_max_attempts,
                exc_info=(failure.type, failure.value, failure.getTracebackObject()),
            )
            return self._deliver_commit_result(failure)

        next_retry_delay = min(retry_delay * REQUEST_RETRY_FACTOR, self.retry_max_delay)

        # Check the retry_delay to see if we should log at the higher level
        # Using attempts % 2 gets us 1-warn/minute with defaults timings
        if retry_delay < self.retry_max_delay or 0 == (attempt % 2):
            log.debug(
                "%r: Failed to commit offset %s (will retry in %.2f seconds)",
                self,
                commit_offset,
                next_retry_delay,
                exc_info=(failure.type, failure.value, failure.getTracebackObject()),
            )
        else:
            # We've retried until we hit the max delay, log alternately at warn
            log.warning(
                "%r: Failed to commit offset %s (will retry in %.2f seconds)",
                self,
                commit_offset,
                next_retry_delay,
                exc_info=(failure.type, failure.value, failure.getTracebackObject()),
            )

        # Schedule a delayed call to retry the commit
        self._commit_call = self.client.reactor.callLater(
            next_retry_delay, self._send_commit_request, next_retry_delay, attempt + 1
        )

    def _handle_auto_commit_error(self, failure):
        if self._start_d is not None and not self._start_d.called:
            self._start_d.errback(failure)

    def _handle_processor_error(self, failure):
        """Handle a failure in the processing of a block of messages

        This method is called when the processor func fails while processing
        a block of messages. Since we can't know how best to handle a
        processor failure, we just :func:`errback` our :func:`start` method's
        deferred to let our user know about the failure.
        """
        # Check if we're stopping/stopped and the errback of the processor
        # deferred is just the cancelling we initiated.  If so, we skip
        # notifying via the _start_d deferred, as it will be 'callback'd at the
        # end of stop()
        if not (self._stopping and failure.check(CancelledError)):
            if self._start_d:  # Make sure we're not already stopped
                self._start_d.errback(failure)

    def _handle_fetch_error(self, failure):
        """A fetch request resulted in an error. Retry after our current delay

        When a fetch error occurs, we check to see if the Consumer is being
        stopped, and if so just return, trapping the CancelledError. If not, we
        check if the Consumer has a non-zero setting for
        :attr:`request_retry_max_attempts` and if so and we have reached that limit we
        errback() the Consumer's start() deferred with the failure. If not, we
        determine whether to log at debug or warning (we log at warning every
        other retry after backing off to the max retry delay, resulting in a
        warning message approximately once per minute with the default timings)
        We then wait our current :attr:`retry_delay`, and retry the fetch. We
        also increase our retry_delay by Apery's constant (1.20205) and note
        the failed fetch by incrementing :attr:`_fetch_attempt_count`.

        NOTE: this may retry forever.
        TODO: Possibly make this differentiate based on the failure
        """
        # The _request_d deferred has fired, clear it.
        self._request_d = None

        if failure.check(OffsetOutOfRangeError):
            if self.auto_offset_reset is None:
                self._start_d.errback(failure)
                return
            self._fetch_offset = self.auto_offset_reset

        if self._stopping and failure.check(CancelledError):
            # Not really an error
            return
        # Do we need to abort?
        if self.request_retry_max_attempts != 0 and self._fetch_attempt_count >= self.request_retry_max_attempts:
            log.debug(
                "%r: Exhausted attempts: %d fetching messages from kafka: %r",
                self,
                self.request_retry_max_attempts,
                failure,
            )
            self._start_d.errback(failure)
            return

        # Decide how to log this failure... If we have retried so many times
        # we're at the retry_max_delay, then we log at warning every other time
        # debug otherwise
        if self.retry_delay < self.retry_max_delay or 0 == (self._fetch_attempt_count % 2):
            log.debug("%r: Failure fetching messages from kafka: %r", self, failure)
        else:
            # We've retried until we hit the max delay, log at warn
            log.warning("%r: Still failing fetching messages from kafka: %r", self, failure)
        self._retry_fetch()

    def _handle_fetch_response(self, responses):
        """The callback handling the successful response from the fetch request

        Delivers the message list to the processor, handles per-message errors
        (ConsumerFetchSizeTooSmall), triggers another fetch request

        If the processor is still processing the last batch of messages, we
        defer this processing until it's done.  Otherwise, we start another
        fetch request and submit the messages to the processor
        """
        # Successful fetch, reset our retry delay
        self.retry_delay = self.retry_init_delay
        self._fetch_attempt_count = 1

        # Check to see if we are still processing the last block we fetched...
        if self._msg_block_d:
            # We are still working through the last block of messages...
            # We have to wait until it's done, then process this response
            self._msg_block_d.addCallback(lambda _: self._handle_fetch_response(responses))
            return

        # No ongoing processing, great, let's get some started.
        # Request no longer outstanding, clear the deferred tracker so we
        # can refetch
        self._request_d = None
        messages = []
        try:
            for resp in responses:  # We should really only ever get one...
                if resp.partition != self.partition:
                    log.warning(
                        "%r: Got response with partition: %r not our own: %r",
                        self,
                        resp.partition,
                        self.partition,
                    )
                    continue
                # resp.messages is a KafkaCodec._decode_message_set_iter
                # Note that 'message' here is really an OffsetAndMessage
                for message in resp.messages:
                    # Check for messages included which are from prior to our
                    # desired offset: can happen due to compressed message sets
                    if message.offset < self._fetch_offset:
                        log.debug(
                            "Skipping message at offset: %d, because its " "offset is less that our fetch offset: %d.",
                            message.offset,
                            self._fetch_offset,
                        )
                        continue
                    # Create a 'SourcedMessage' and add it to the messages list
                    messages.append(
                        SourcedMessage(
                            message=message.message,
                            offset=message.offset,
                            topic=self.topic,
                            partition=self.partition,
                        )
                    )
                    # Update our notion of from where to fetch.
                    self._fetch_offset = message.offset + 1
        except ConsumerFetchSizeTooSmall:
            # A message was too large for us to receive, given our current
            # buffer size. Grow it until it works, or we hit our max
            # Grow by 16x up to 1MB (could result in 16MB buf), then by 2x
            factor = 2
            if self.buffer_size <= 2**20:
                factor = 16
            if self.max_buffer_size is None:
                # No limit, increase until we succeed or fail to alloc RAM
                self.buffer_size *= factor
            elif self.max_buffer_size is not None and self.buffer_size < self.max_buffer_size:
                # Limited, but currently below it.
                self.buffer_size = min(self.buffer_size * factor, self.max_buffer_size)
            else:
                # We failed, and are already at our max. Nothing we can do but
                # create a Failure and errback() our start() deferred
                log.error("Max fetch size %d too small", self.max_buffer_size)
                failure = Failure(
                    ConsumerFetchSizeTooSmall("Max buffer size:%d too small for message", self.max_buffer_size)
                )
                self._start_d.errback(failure)
                return

            log.debug(
                "Next message larger than fetch size, increasing " "to %d (~2x) and retrying",
                self.buffer_size,
            )

        finally:
            # If we were able to extract any messages, deliver them to the
            # processor now.
            if messages:
                self._msg_block_d = Deferred()
                self._process_messages(messages)

        # start another fetch, if needed, but use callLater to avoid recursion
        self._retry_fetch(0)

    @inlineCallbacks
    def _process_messages(self, messages):
        """Send messages to the `processor` callback to be processed

        In the case we have a commit policy, we send messages to the processor
        in blocks no bigger than auto_commit_every_n (if set). Otherwise, we
        send the entire message block to be processed.

        """
        # Default to processing the entire block...
        proc_block_size = sys.maxsize
        # Unless our auto commit_policy restricts us to process less
        if self.auto_commit_every_n:
            proc_block_size = self.auto_commit_every_n

        proc_block_begin = 0
        proc_block_end = proc_block_size

        while proc_block_begin < len(messages) and not self._shuttingdown:
            msgs_to_proc = messages[proc_block_begin:proc_block_end]
            # Call our processor callable and handle the possibility it returned
            # a deferred...
            last_offset = msgs_to_proc[-1].offset
            self._processor_d = d = maybeDeferred(self.processor, self, msgs_to_proc)
            # Once the processor completes, clear our _processor_d
            d.addBoth(self._clear_processor_deferred)
            # Record the offset of the last processed message and check autocommit
            d.addCallback(self._update_processed_offset, last_offset)
            # Add an error handler
            d.addErrback(self._handle_processor_error)
            # If we were stopped, cancel the processor deferred. Note, we have to
            # do this here, in addition to in stop() because the processor func
            # itself could have called stop(), and then when it returned, we re-set
            # self._processor_d to the return of maybeDeferred().
            if self._stopping or self._start_d is None:
                d.cancel()
                break
            else:
                yield d
                proc_block_begin = proc_block_end
                proc_block_end += proc_block_size

        # We're done with this block. If we had another fetch result
        # waiting, this callback will trigger the processing thereof.
        if self._msg_block_d:
            _msg_block_d, self._msg_block_d = self._msg_block_d, None
            _msg_block_d.callback(True)

    def _do_fetch(self):
        """Send a fetch request if there isn't a request outstanding

        Sends a fetch request to the Kafka cluster to get messages at the
        current offset.  When the response comes back, if there are messages,
        it delivers them to the :attr:`processor` callback and initiates
        another fetch request.  If there is a recoverable error, the fetch is
        retried after :attr:`retry_delay`.

        In the case of an unrecoverable error, :func:`errback` is called on the
        :class:`Deferred` returned by :meth:`start()`.
        """
        # Check for outstanding request.
        if self._request_d:
            log.debug("_do_fetch: Outstanding request: %r", self._request_d)
            return

        # Cleanup our _retry_call, if we have one
        if self._retry_call is not None:
            if self._retry_call.active():
                self._retry_call.cancel()
            self._retry_call = None

        # Do we know our offset yet, or do we need to figure it out?
        if self._fetch_offset == OFFSET_EARLIEST or self._fetch_offset == OFFSET_LATEST:
            # We need to fetch the offset for our topic/partition
            offset_request = OffsetRequest(self.topic, self.partition, self._fetch_offset, 1)
            self._request_d = self.client.send_offset_request([offset_request])
            self._request_d.addCallbacks(self._handle_offset_response, self._handle_offset_error)
        elif self._fetch_offset == OFFSET_COMMITTED:
            # We need to fetch the committed offset for our topic/partition
            # Note we use the same callbacks, as the responses are "close
            # enough" for our needs here
            if not self.consumer_group:
                # consumer_group must be set for OFFSET_COMMITTED
                failure = Failure(InvalidConsumerGroupError("Bad Group_id:{0!r}".format(self.consumer_group)))
                self._start_d.errback(failure)
            request = OffsetFetchRequest(self.topic, self.partition)
            self._request_d = self.client.send_offset_fetch_request(self.consumer_group, [request])
            self._request_d.addCallbacks(self._handle_offset_response, self._handle_offset_error)
        else:
            # Create fetch request payload for our partition
            request = FetchRequest(self.topic, self.partition, self._fetch_offset, self.buffer_size)
            # Send request and add handlers for the response
            self._request_d = self.client.send_fetch_request(
                [request],
                max_wait_time=self.fetch_max_wait_time,
                min_bytes=self.fetch_min_bytes,
            )
            # We need a temp for this because if the response is already
            # available, _handle_fetch_response() will clear self._request_d
            d = self._request_d
            d.addCallback(self._handle_fetch_response)
            d.addErrback(self._handle_fetch_error)

    def _commit_timer_failed(self, fail):
        """Handle an error in the commit() function

        Our commit() function called by the LoopingCall failed. Some error
        probably came back from Kafka and _check_error() raised the exception
        For now, just log the failure and restart the loop
        """
        log.warning(
            "_commit_timer_failed: uncaught error %r: %s in _auto_commit",
            fail,
            fail.getBriefTraceback(),
        )
        self._commit_looper_d = self._commit_looper.start(self.auto_commit_every_s, now=False)

    def _commit_timer_stopped(self, lCall):
        """We're shutting down, clean up our looping call..."""
        if self._commit_looper is not lCall:
            log.warning(
                "_commit_timer_stopped with wrong timer:%s not:%s",
                lCall,
                self._commit_looper,
            )
        else:
            log.debug("_commit_timer_stopped: %s %s", lCall, self._commit_looper)
            self._commit_looper = None
            self._commit_looper_d = None
