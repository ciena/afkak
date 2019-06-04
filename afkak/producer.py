# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2016, 2017, 2018, 2019 Ciena Corporation

from __future__ import absolute_import

import logging
from collections import defaultdict
from numbers import Integral

from twisted.internet.defer import CancelledError as tid_CancelledError
from twisted.internet.defer import (
    Deferred, DeferredList, fail, inlineCallbacks, returnValue, succeed,
)
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure

from ._util import _coerce_topic
from .common import (
    CODEC_NONE, PRODUCER_ACK_LOCAL_WRITE, PRODUCER_ACK_NOT_REQUIRED,
    CancelledError, FailedPayloadsError, KafkaError, NoResponseError,
    NotLeaderForPartitionError, ProduceRequest, SendRequest, TopicAndPartition,
    UnknownTopicOrPartitionError, UnsupportedCodecError, _check_error,
)
from .kafkacodec import _SUPPORTED_CODECS, create_message_set
from .partitioner import RoundRobinPartitioner

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

BATCH_SEND_SECS_COUNT = 30  # Seconds
BATCH_SEND_MSG_COUNT = 10  # Messages
BATCH_SEND_MSG_BYTES = 32 * 1024  # 32 KBytes


class Producer(object):
    """Write messages to Kafka with retries and batching

    :param client: `KafkaClient` instance to use

    :param partitioner_class:
        Factory for topic partitioners, a callable that accepts a topic and
        list of partition numbers. The default is
        :class:`~afkak.partitioner.RoundRobinPartitioner`.

    :param int req_acks:
        A value indicating the acknowledgements that the server must
        receive before responding to the request

    :param float ack_timeout:
        Value (in milliseconds) indicating a how long the server can wait for
        the above acknowledgements.

    :param int max_req_attempts:
        Number of times we will retry a request to Kafka before failing the
        request.

    :param float retry_interval:
        Initial retry interval in seconds, defaults to INIT_RETRY_INTERVAL.

    :param codec:
        Compression codec to apply to messages. Default: `CODEC_NONE`.

    :param bool batch_send:
        If True, messages are sent in batches.

    :param int batch_every_n:
        If set, messages are sent in batches of this many messages.

    :param int batch_every_b:
        If set, messages are sent when this many bytes of messages are waiting
        to be sent.

    :param int batch_every_t:
        If set, messages are sent after this many seconds (even if waiting for
        other conditions to apply).  This caps the latency automatic batching
        incurs.
    """

    DEFAULT_ACK_TIMEOUT = 1000  # How long the server should wait (msec)
    DEFAULT_REQ_ATTEMPTS = 10  # Send request up to 10 times before failing
    INIT_RETRY_INTERVAL = 0.25  # Initial retry interval in seconds
    RETRY_INTERVAL_FACTOR = 1.20205  # Factor by which we increase our delay

    _sendLooper = None
    _sendLooperD = None

    def __init__(self, client,
                 partitioner_class=RoundRobinPartitioner,
                 req_acks=PRODUCER_ACK_LOCAL_WRITE,
                 ack_timeout=DEFAULT_ACK_TIMEOUT,
                 max_req_attempts=DEFAULT_REQ_ATTEMPTS,
                 retry_interval=INIT_RETRY_INTERVAL,
                 codec=None,
                 batch_send=False,
                 batch_every_n=BATCH_SEND_MSG_COUNT,
                 batch_every_b=BATCH_SEND_MSG_BYTES,
                 batch_every_t=BATCH_SEND_SECS_COUNT):

        # When messages are sent, the partition of the message is picked
        # by the partitioner object for that topic. The partitioners are
        # created as needed from the "partitioner_class" class and stored
        # by topic in self.partitioners
        self.partitioner_class = partitioner_class
        self.partitioners = {}

        # Set our client, our acks/timeouts, our clock & interval
        self.client = client
        self.req_acks = req_acks
        self.ack_timeout = ack_timeout
        self._max_attempts = max_req_attempts
        self._req_attempts = 0
        self._retry_interval = self._init_retry_interval = retry_interval

        # For efficiency, the producer can be set to send messages in
        # batches. In that case, the producer will wait until at least
        # batch_every_n messages are waiting to be sent, or batch_every_b
        # bytes of messages are waiting to be sent, or it has been
        # batch_every_t seconds since the last send
        if not batch_send:
            self.batchDesc = "Unbatched"
            self.batch_every_n = 1
            self.batch_every_b = 1
            self.batch_every_t = None
        else:
            if not isinstance(batch_every_n, Integral):
                msg = "batch_every_n: {0!r} unsupported".format(batch_every_n)
                raise TypeError(msg)
            if not isinstance(batch_every_b, Integral):
                msg = "batch_every_b: {0!r} unsupported".format(batch_every_b)
                raise TypeError(msg)
            self.batch_every_n = batch_every_n
            self.batch_every_b = batch_every_b
            self.batch_every_t = batch_every_t
            self.batchDesc = "{}cnt/{}bytes/{}secs".format(
                batch_every_n, batch_every_b, batch_every_t)
            if batch_every_t:
                self._sendLooper = LoopingCall(self._send_batch)
                self._sendLooper.clock = self.client.reactor
                self._sendLooperD = self._sendLooper.start(
                    batch_every_t, now=False)
                self._sendLooperD.addCallbacks(self._send_timer_stopped,
                                               self._send_timer_failed)

        # Current batch reqs & msgs/bytes, and all outstanding reqs
        self._batch_reqs = []  # Current batch (possibly of 1 for unbatched)
        self._waitingMsgCount = 0
        self._waitingByteCount = 0
        self._outstanding = set([])  # All currently outstanding requests
        self._batch_send_d = None  # Outstanding client request to send msgs

        # Are we compressing messages, or just sending 'raw'?
        if codec is None:
            codec = CODEC_NONE
        elif codec not in _SUPPORTED_CODECS:
            if not isinstance(codec, Integral):
                raise TypeError("Codec: %r unsupported" % codec)
            raise UnsupportedCodecError("Codec 0x%02x unsupported" % codec)
        self.codec = codec

    def __repr__(self):
        return '<Producer {}:{}:{}:{}>'.format(self.partitioner_class,
                                               self.batchDesc, self.req_acks,
                                               self.ack_timeout)

    def send_messages(self, topic, key=None, msgs=()):
        """
        Given a topic, and optional key (for partitioning) and a list of
        messages, send them to Kafka, either immediately, or when a batch is
        ready, depending on the Producer's batch settings.

        :param str topic: Kafka topic to send the messages to

        :param str key:
            Message key used to determine the topic partition to which the
            messages will be written. Either `bytes` or `None`.

            `None` means that there is no key, but note that that:

            - Kafka does not permit producing unkeyed messages to a compacted topic.
            - The *partitioner_class* may require a non-`None` key
              (`HashedPartitioner` does so).

        :param list msgs:
            A non-empty sequence of message bytestrings to send. `None`
            indicates a ``null`` message (i.e. a tombstone on a compacted
            topic).

        :returns:
            A :class:`~twisted.internet.defer.Deferred` that fires when the
            messages have been received by the Kafka cluster.

            It will fail with `TypeError` when:

            - *topic* is not text (`str` on Python 3, `str` or `unicode` on Python 2)
            - *key* is not `bytes` or `None`
            - *msgs* is not a sequence of `bytes` or `None`

            It will fail with `ValueError` when *msgs* is empty.
        """
        try:
            topic = _coerce_topic(topic)
            if key is not None and not isinstance(key, bytes):
                raise TypeError('key={!r} must be bytes or None'.format(key))

            if not msgs:
                raise ValueError("msgs must be a non-empty sequence")

            msg_cnt = len(msgs)
            byte_cnt = 0
            for index, m in enumerate(msgs):
                if m is None:
                    continue

                if isinstance(m, tuple):
                    ts, m = m

                if not isinstance(m, bytes):
                    raise TypeError('Message {} to topic {} ({!r:.100}) has type {}, but must have type {}'.format(
                        index, topic, m, type(m).__name__, type(bytes).__name__))

                byte_cnt += len(m)
        except Exception:
            return fail()

        d = Deferred(self._cancel_send_messages)
        self._batch_reqs.append(SendRequest(topic, key, msgs, d))
        self._waitingMsgCount += msg_cnt
        self._waitingByteCount += byte_cnt

        # Add request to list of outstanding reqs' callback to remove
        self._outstanding.add(d)
        d.addBoth(self._remove_from_outstanding, d)
        # See if we have enough messages in the batch to do a send.
        self._check_send_batch()
        return d

    def stop(self):
        """
        Terminate any outstanding requests.

        :returns: :class:``Deferred` which fires when fully stopped.
        """
        self.stopping = True
        # Cancel any outstanding request to our client
        if self._batch_send_d:
            self._batch_send_d.cancel()
        # Do we have to worry about our looping call?
        if self.batch_every_t is not None:
            # Stop our looping call, and wait for the deferred to be called
            if self._sendLooper is not None:
                self._sendLooper.stop()
        # Make sure requests that wasn't cancelled above are now
        self._cancel_outstanding()
        return self._sendLooperD or succeed(None)

    # # Private Methods # #

    def _send_timer_failed(self, fail):
        """
        Our _send_batch() function called by the LoopingCall failed. Some
        error probably came back from Kafka and _check_error() raised the
        exception
        For now, just log the failure and restart the loop
        """
        log.warning('_send_timer_failed:%r: %s', fail,
                    fail.getBriefTraceback())
        self._sendLooperD = self._sendLooper.start(
            self.batch_every_t, now=False)

    def _send_timer_stopped(self, lCall):
        """
        We're shutting down, clean up our looping call...
        """
        if self._sendLooper is not lCall:
            log.warning('commitTimerStopped with wrong timer:%s not:%s',
                        lCall, self._sendLooper)
        else:
            self._sendLooper = None
            self._sendLooperD = None

    @inlineCallbacks
    def _next_partition(self, topic, key=None):
        """get the next partition to which to publish

        Check with our client for the latest partitions for the topic, then
        ask our partitioner for the next partition to which we should publish
        for the give key. If needed, create a new partitioner for the topic.
        """
        # check if the client has metadata for the topic
        while self.client.metadata_error_for_topic(topic):
            # client doesn't have good metadata for topic. ask to fetch...
            # check if we have request attempts left
            if self._req_attempts >= self._max_attempts:
                # No, no attempts left, so raise the error
                # FIXME: This can be deceptive. metadata_error_for_topic()
                # returns the error code for UnknownTopicOrPartitonError when
                # nothing is known about the topic.
                _check_error(self.client.metadata_error_for_topic(topic))
            yield self.client.load_metadata_for_topics(topic)
            if not self.client.metadata_error_for_topic(topic):
                break
            self._req_attempts += 1
            d = Deferred()
            self.client.reactor.callLater(
                self._retry_interval, d.callback, True)
            self._retry_interval *= self.RETRY_INTERVAL_FACTOR
            yield d

        # Ok, should be safe to get the partitions now...
        partitions = self.client.topic_partitions[topic]
        # Do we have a partitioner for this topic already?
        if topic not in self.partitioners:
            # No, create a new paritioner for topic, partitions
            self.partitioners[topic] = \
                self.partitioner_class(topic, partitions)
        # Lookup the next partition
        partition = self.partitioners[topic].partition(key, partitions)
        returnValue(partition)

    def _send_requests(self, parts_results, requests):
        """Send the requests

        We've determined the partition for each message group in the batch, or
        got errors for them.
        """
        # We use these dictionaries to be able to combine all the messages
        # destined to the same topic/partition into one request
        # the messages & deferreds, both by topic+partition
        reqsByTopicPart = defaultdict(list)
        payloadsByTopicPart = defaultdict(list)
        deferredsByTopicPart = defaultdict(list)

        # We now have a list of (succeeded/failed, partition/None) tuples
        # for the partition lookups we did on each message group, zipped with
        # the requests
        for (success, part_or_failure), req in zip(parts_results, requests):
            if req.deferred.called:
                # Submitter cancelled the request while we were waiting for
                # the topic/partition, skip it
                continue
            if not success:
                # We failed to get a partition for this request, errback to the
                # caller with the failure. Maybe this should retry? However,
                # since this failure is likely to affect an entire Topic, there
                # should be no issues with ordering of messages within a
                # partition of a topic getting out of order. Let the caller
                # retry the particular request if they like, or they could
                # cancel all their outstanding requests in
                req.deferred.errback(part_or_failure)
                continue
            # Ok, we now have a partition for this request, we can add the
            # request for this topic/partition to reqsByTopicPart, and the
            # caller's deferred to deferredsByTopicPart
            topicPart = TopicAndPartition(req.topic, part_or_failure)
            reqsByTopicPart[topicPart].append(req)
            deferredsByTopicPart[topicPart].append(req.deferred)

        # Build list of payloads grouped by topic/partition
        # That is, we bundle all the messages destined for a given
        # topic/partition, even if they were submitted by different
        # requests into a single 'payload', and then we submit all the
        # payloads as a list to the client for sending to the various
        # brokers. The finest granularity of success/failure is at the
        # payload (topic/partition) level.
        payloads = []
        for (topic, partition), reqs in reqsByTopicPart.items():
            msgSet = create_message_set(reqs, self.codec)
            req = ProduceRequest(topic, partition, msgSet)
            topicPart = TopicAndPartition(topic, partition)
            payloads.append(req)
            payloadsByTopicPart[topicPart] = req
        # Make sure we have some payloads to send
        if not payloads:
            return
        # send the request
        d = self.client.send_produce_request(
            payloads, acks=self.req_acks, timeout=self.ack_timeout,
            fail_on_error=False)
        self._req_attempts += 1
        # add our handlers
        d.addBoth(self._handle_send_response, payloadsByTopicPart,
                  deferredsByTopicPart)
        return d

    def _complete_batch_send(self, resp):
        """Complete the processing of our batch send operation

        Clear the deferred tracking our current batch processing
        and reset our retry count and retry interval
        Return none to eat any errors coming from up the deferred chain
        """
        self._batch_send_d = None
        self._req_attempts = 0
        self._retry_interval = self._init_retry_interval
        if isinstance(resp, Failure) and not resp.check(tid_CancelledError,
                                                        CancelledError):
            log.error("Failure detected in _complete_batch_send: %r\n%r",
                      resp, resp.getTraceback())
        return

    def _check_send_batch(self, result=None):
        """Check if we have enough messages/bytes to send
        Since this can be called from the callback chain, we
        pass through our first (non-self) arg
        """
        if (
            (self.batch_every_n and self.batch_every_n <= self._waitingMsgCount) or
            (self.batch_every_b and self.batch_every_b <= self._waitingByteCount)
        ):
            self._send_batch()
        return result

    def _send_batch(self):
        """
        Send the waiting messages, if there are any, and we can...

        This is called by our LoopingCall every send_every_t interval, and
        from send_messages everytime we have enough messages to send.
        This is also called from py:method:`send_messages` via
        py:method:`_check_send_batch` if there are enough messages/bytes
        to require a send.
        Note, the send will be delayed (triggered by completion or failure of
        previous) if we are currently trying to complete the last batch send.
        """
        # We can be triggered by the LoopingCall, and have nothing to send...
        # Or, we've got SendRequest(s) to send, but are still processing the
        # previous batch...
        if (not self._batch_reqs) or self._batch_send_d:
            return

        # Save a local copy, and clear the global list & metrics
        requests, self._batch_reqs = self._batch_reqs, []
        self._waitingByteCount = 0
        self._waitingMsgCount = 0

        # Iterate over them, fetching the partition for each message batch
        d_list = []
        for req in requests:
            # For each request, we get the topic & key and use that to lookup
            # the next partition on which we should produce
            d_list.append(self._next_partition(req.topic, req.key))
        d = self._batch_send_d = Deferred()
        # Since DeferredList doesn't propagate cancel() calls to deferreds it
        # might be waiting on for a result, we need to use this structure,
        # rather than just using the DeferredList directly
        d.addCallback(lambda r: DeferredList(d_list, consumeErrors=True))
        d.addCallback(self._send_requests, requests)
        # Once we finish fully processing the current batch, clear the
        # _batch_send_d and check if any more requests piled up when we
        # were busy.
        d.addBoth(self._complete_batch_send)
        d.addBoth(self._check_send_batch)
        # Fire off the callback to start processing...
        d.callback(None)

    def _cancel_send_messages(self, d):
        """Cancel a `send_messages` request
        First check if the request is in a waiting batch, of so, great, remove
        it from the batch. If it's not found, we errback() the deferred and
        the downstream processing steps take care of aborting further
        processing.
        We check if there's a current _batch_send_d to determine where in the
        chain we were (getting partitions, or already sent request to Kafka)
        and errback differently.
        """
        # Is the request in question in an unsent batch?
        for req in self._batch_reqs:
            if req.deferred == d:
                # Found the request, remove it and return.
                msgs = req.messages
                self._waitingMsgCount -= len(msgs)
                for m in (_m for _m in msgs if _m is not None):
                    self._waitingByteCount -= len(m)
                # This _should_ be safe as we abort the iteration upon removal
                self._batch_reqs.remove(req)
                d.errback(CancelledError(request_sent=False))
                return

        # If it wasn't found in the unsent batch. We just rely on the
        # downstream processing of the request to check if the deferred
        # has been called and skip further processing for this request
        # Errback the deferred with whether or not we sent the request
        # to Kafka already
        d.errback(
            CancelledError(request_sent=(self._batch_send_d is not None)))
        return

    def _handle_send_response(self, result, payloadsByTopicPart,
                              deferredsByTopicPart):
        """Handle the response from our client to our send_produce_request

        This is a bit complex. Failures can happen in a few ways:

          1. The client sent an empty list, False, None or some similar thing
             as the result, but we were expecting real responses.
          2. The client had a failure before it even tried sending any requests
             to any brokers.

               a. Kafka error: See if we can retry the whole request
               b. Non-kafka: Figure it's a programming error, fail all deferreds

          3. The client sent all the requests (it's all or none) to the brokers
             but one or more request failed (timed out before receiving a
             response, or the brokerclient threw some sort of exception on send
             In this case, the client throws FailedPayloadsError, and attaches
             the responses (NOTE: some can have errors!), and the payloads
             where the send itself failed to the exception.
          4. The client sent all the requests, all responses were received, but
             the Kafka broker indicated an error with servicing the request on
             some of the responses.
        """

        def _deliver_result(d_list, result=None):
            """Possibly callback each deferred in a list with single result"""
            for d in d_list:
                if not isinstance(d, Deferred):
                    # nested list...
                    _deliver_result(d, result)
                else:
                    # We check d.called since the request could have been
                    # cancelled while we waited for the response
                    if not d.called:
                        d.callback(result)

        def _do_retry(payloads):
            # We use 'fail_on_error=False' because we want our client to
            # process every response that comes back from the brokers so
            # we can determine which requests were successful, and which
            # failed for retry
            d = self.client.send_produce_request(
                payloads, acks=self.req_acks, timeout=self.ack_timeout,
                fail_on_error=False)
            self._req_attempts += 1
            # add our handlers
            d.addBoth(self._handle_send_response, payloadsByTopicPart,
                      deferredsByTopicPart)
            return d

        def _cancel_retry(failure, dc):
            # Cancel the retry callLater and pass-thru the failure
            dc.cancel()
            # cancel all the top-level deferreds associated with the request
            _deliver_result(deferredsByTopicPart.values(), failure)
            return failure

        def _check_retry_payloads(failed_payloads_with_errs):
            """Check our retry count and retry after a delay or errback

            If we have more retries to try, create a deferred that will fire
            with the result of delayed retry. If not, errback the remaining
            deferreds with failure

            Params:
            failed_payloads - list of (payload, failure) tuples
            """
            # Do we have retries left?
            if self._req_attempts >= self._max_attempts:
                # No, no retries left, fail each failed_payload with its
                # associated failure
                for p, f in failed_payloads_with_errs:
                    t_and_p = TopicAndPartition(p.topic, p.partition)
                    _deliver_result(deferredsByTopicPart[t_and_p], f)
                return
            # Retries remain!  Schedule one...
            d = Deferred()
            dc = self.client.reactor.callLater(
                self._retry_interval, d.callback, [p for p, f in
                                                   failed_payloads])
            self._retry_interval *= self.RETRY_INTERVAL_FACTOR
            # Cancel the callLater when request is cancelled before it fires
            d.addErrback(_cancel_retry, dc)

            # Reset the topic metadata for all topics which had failed_requests
            # where the failures were of the kind UnknownTopicOrPartitionError
            # or NotLeaderForPartitionError, since those indicate our client's
            # metadata is out of date.
            reset_topics = set()
            for payload, e in failed_payloads:
                if (isinstance(e, NotLeaderForPartitionError) or
                        isinstance(e, UnknownTopicOrPartitionError)):
                    reset_topics.add(payload.topic)
            if reset_topics:
                self.client.reset_topic_metadata(*reset_topics)

            d.addCallback(_do_retry)
            return d

        # The payloads we need to retry, if we still can..
        failed_payloads = []
        # In the case we are sending requests without requiring acks, the
        # brokerclient will immediately callback() the deferred upon send with
        # None. In that case, we just iterate over all the deferreds in
        # deferredsByTopicPart and callback them with None
        # If we are expecting responses/acks, and we get an empty result, we
        # callback with a Failure of NoResponseError
        if not result:
            # Success, but no results, is that what we're expecting?
            if self.req_acks == PRODUCER_ACK_NOT_REQUIRED:
                result = None
            else:
                # We got no result, but we were expecting one? Fail everything!
                result = Failure(NoResponseError())
            _deliver_result(deferredsByTopicPart.values(), result)
            return
        elif isinstance(result, Failure):
            # Failure!  Was it total, or partial?
            if not result.check(FailedPayloadsError):
                # Total failure of some sort!
                # The client was unable to send the request at all. If it's
                # a KafkaError (probably Leader/Partition unavailable), retry
                if result.check(KafkaError):
                    # Yep, a kafak error. Set failed_payloads, and we'll retry
                    # them all below. Set failure for errback to callers if we
                    # are all out of retries
                    failure, result = result, []  # no succesful results, retry
                    failed_payloads = [(p, failure) for p in
                                       payloadsByTopicPart.values()]
                else:
                    # Was the request cancelled?
                    if not result.check(tid_CancelledError):
                        # Uh Oh, programming error? Log it!
                        log.error("Unexpected failure: %r in "
                                  "_handle_send_response", result)
                    # Cancelled, or programming error, we fail the requests
                    _deliver_result(deferredsByTopicPart.values(), result)
                    return
            else:
                # FailedPayloadsError: This means that some/all of the
                # requests to a/some brokerclients failed to send.
                # Pull the successful responses and the failed_payloads off
                # the exception and handle them below. Preserve the
                # FailedPayloadsError as 'failure'
                failure = result
                result = failure.value.args[0]
                failed_payloads = failure.value.args[1]

        # Do we have results? Iterate over them and if the response indicates
        # success, then callback the associated deferred. If the response
        # indicates an error, then setup that request for retry.
        # NOTE: In this case, each failed_payload get it's own error...
        for res in result:
            t_and_p = TopicAndPartition(res.topic, res.partition)
            t_and_p_err = _check_error(res, raiseException=False)
            if not t_and_p_err:
                # Success for this topic/partition
                d_list = deferredsByTopicPart[t_and_p]
                _deliver_result(d_list, res)
            else:
                p = payloadsByTopicPart[t_and_p]
                failed_payloads.append((p, t_and_p_err))

        # Were there any failed requests to possibly retry?
        if failed_payloads:
            return _check_retry_payloads(failed_payloads)
        return

    def _remove_from_outstanding(self, result, d):
        """ Remove 'd' from the list of outstanding requests"""
        self._outstanding.remove(d)
        return result

    def _cancel_outstanding(self):
        """Cancel all of our outstanding requests"""
        for d in list(self._outstanding):
            d.addErrback(lambda _: None)  # Eat any uncaught errors
            d.cancel()
