# -*- coding: utf-8 -*-
# Copyright 2017, 2018, 2019 Ciena Corporation.
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

import collections
import itertools
import logging

from afkak.common import (
    OFFSET_COMMITTED,
    CoordinatorNotAvailable,
    IllegalGeneration,
    InconsistentGroupProtocol,
    InvalidGroupId,
    KafkaError,
    NotCoordinatorForConsumerError,
    RebalanceInProgress,
    RequestTimedOutError,
    RestartError,
    RestopError,
    UnknownMemberId,
    _HeartbeatRequest,
    _JoinGroupRequest,
    _JoinGroupRequestProtocol,
    _LeaveGroupRequest,
    _SyncGroupRequest,
    _SyncGroupRequestMember,
)
from afkak.consumer import Consumer
from afkak.kafkacodec import KafkaCodec

import attr
from twisted.internet.defer import CancelledError, Deferred, DeferredList, inlineCallbacks
from twisted.internet.task import LoopingCall

log = logging.getLogger(__name__)


class Coordinator(object):
    """
    Private group coordinator implementation.

    You almost certainly want to use :class:`afkak.ConsumerGroup` instead.
    The parameters this class takes are documented there.

    """

    def __init__(
        self,
        client,
        group_id,
        topics,
        session_timeout_ms=30000,
        heartbeat_interval_ms=5000,
        initial_backoff_ms=1000,
        retry_backoff_ms=100,
        fatal_backoff_ms=10000,
    ):
        self.client = client
        self.group_id = group_id
        self.topics = topics
        if not topics:
            raise ValueError("topics must be non-empty")
        self.session_timeout_ms = session_timeout_ms
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.initial_backoff_ms = initial_backoff_ms
        self.retry_backoff_ms = retry_backoff_ms
        self.fatal_backoff_ms = fatal_backoff_ms

        self.member_id = ""
        self.leader_id = None
        self.generation_id = None
        self.coordinator_broker = None  # BrokerMetadata of the coordinator
        self.protocol = _ConsumerProtocol()

        self._start_d = None
        self._state = "[initialized]"  # Keep track of state for debugging
        # do we need to rejoin?
        self._rejoin_needed = True
        # are we shutting down?
        self._stopping = False
        # delayedcall for a pending rejoin
        self._rejoin_wait_dc = None
        # deferred for a rejoin in progress
        self._rejoin_d = False

        # loopingcall for the heartbeat timer
        self._heartbeat_looper = LoopingCall(self._heartbeat)
        self._heartbeat_looper.clock = self.client.reactor
        self._heartbeat_looper_d = None
        self._heartbeat_request_d = None

    def __repr__(self):
        return "<{}.{} group={} member={} topics={} {}>".format(
            __name__,
            self.__class__.__name__,
            self.group_id,
            self.member_id,
            self.topics,
            self._state,
        )

    def get_coordinator_broker(self):
        def _get_coordinator_failed(result):
            retry_delay = self.initial_backoff_ms

            if result.check(
                CoordinatorNotAvailable,
                NotCoordinatorForConsumerError,
                RequestTimedOutError,
            ):
                # while kafka is starting up, it may take some time before it
                # successfully chooses an authoritative broker for the group
                log.debug("%s could not get coordinator broker: %s", self, result.value)
                if result.check(RequestTimedOutError):
                    retry_delay = self.fatal_backoff_ms

            elif result.check(KafkaError):
                log.warn("%s could not get coordinator broker: %s", self, result.value)
                retry_delay = self.fatal_backoff_ms
            else:
                return result

            self.client.reactor.callLater(
                retry_delay / 1000.0,
                self.join_and_sync,
            )
            return

        def _get_coordinator_success(leader):
            if not leader:
                self.client.reactor.callLater(
                    self.initial_backoff_ms / 1000.0,
                    self.join_and_sync,
                )
                return
            # do a topic metadata load, because if we are elected leader
            # we will need to know the partition info for all our topics
            # to assign them correctly
            metadata_d = self.client.load_metadata_for_topics(*self.topics)
            metadata_d.addCallback(lambda result: self)
            return metadata_d

        d = self.client._get_coordinator_for_group(self.group_id)
        d.addCallbacks(_get_coordinator_success, _get_coordinator_failed)
        return d

    def send_join_group_request(self):
        payload = _JoinGroupRequest(
            group=self.group_id,
            session_timeout=self.session_timeout_ms,
            member_id=self.member_id,
            protocol_type=self.protocol.protocol_type,
            group_protocols=self.protocol.join_group_protocols(self.topics),
        )

        def _join_group_success(response):
            self.member_id = response.member_id
            self.generation_id = response.generation_id
            self.leader_id = response.leader_id
            return response

        de = self.client._send_request_to_coordinator(
            self.group_id,
            payload,
            encoder_fn=KafkaCodec.encode_join_group_request,
            decode_fn=KafkaCodec.decode_join_group_response,
            # join_group requests can take up to 30s as the group restabilizes
            # override client.timeout to allow for that plus some extra
            min_timeout=35.0,
        )
        de.addCallbacks(
            _join_group_success,
            self.rejoin_after_error,
            errbackKeywords=dict(label="join_group"),
        )
        return de

    def send_sync_group_request(self, group_assignment):
        # Make the sync group request
        payload = _SyncGroupRequest(
            group=self.group_id,
            generation_id=self.generation_id,
            member_id=self.member_id,
            group_assignment=group_assignment,
        )
        de = self.client._send_request_to_coordinator(
            group=self.group_id,
            payload=payload,
            encoder_fn=KafkaCodec.encode_sync_group_request,
            decode_fn=KafkaCodec.decode_sync_group_response,
        )
        de.addErrback(self.rejoin_after_error, label="sync_group")
        return de

    def send_leave_group_request(self):
        # Make the leave group request
        payload = _LeaveGroupRequest(
            group=self.group_id,
            member_id=self.member_id,
        )

        def _leave_group_success(result):
            log.debug("leave_group request succeeded")
            self.member_id = ""
            self.generation_id = None

        de = self.client._send_request_to_coordinator(
            group=self.group_id,
            payload=payload,
            encoder_fn=KafkaCodec.encode_leave_group_request,
            decode_fn=KafkaCodec.decode_leave_group_response,
        )
        de.addCallback(_leave_group_success)
        return de

    def send_heartbeat_request(self):
        # Make the heartbeat request
        payload = _HeartbeatRequest(
            group=self.group_id,
            generation_id=self.generation_id,
            member_id=self.member_id,
        )
        request_d = self.client._send_request_to_coordinator(
            group=self.group_id,
            payload=payload,
            encoder_fn=KafkaCodec.encode_heartbeat_request,
            decode_fn=KafkaCodec.decode_heartbeat_response,
        )
        return request_d

    def start(self):
        if self._start_d:
            raise RestartError("Start called on already-started coordinator")

        log.debug("starting")
        self._start_d = Deferred()
        self.join_and_sync()
        return self._start_d

    def reset_heartbeat_timer(self):
        if self._heartbeat_looper.running:
            self._heartbeat_looper.reset()
        else:
            self._heartbeat_looper_d = self._heartbeat_looper.start(
                self.heartbeat_interval_ms / 1000.0,
                now=False,
            )
            self._heartbeat_looper_d.addErrback(self._heartbeat_timer_failed)
            self._heartbeat_looper_d.addBoth(self._heartbeat_timer_stopped)

    @inlineCallbacks
    def stop(self, errback_result=None):
        if self._start_d is None:
            raise RestopError("Shutdown called on non-running coordinator")

        if self._stopping:
            raise RestopError("Shutdown called more than once.")

        log.info("%s stopping with %s", self, errback_result)
        self._state = "[stopping]"
        self._stopping = True
        self._rejoin_needed = False
        if self._rejoin_wait_dc:
            self._rejoin_wait_dc.cancel()

        if self._heartbeat_request_d:
            self._heartbeat_request_d.cancel()

        if self._heartbeat_looper:
            if self._heartbeat_looper.running:
                self._heartbeat_looper.stop()

        self._state = "[leaving]"
        if self.coordinator_broker is not None and self.member_id:
            try:
                yield self.send_leave_group_request()
            except Exception:
                log.exception("error sending leave group request")

        if self._rejoin_d:
            self._rejoin_d, d = None, self._rejoin_d
            d.cancel()

        self._state = "[stopped]"
        self.protocol = None
        self.member_id = ""
        self.generation_id = None
        self.coordinator_broker = None

        self._start_d, d = None, self._start_d
        if not d.called:
            if errback_result:
                d.errback(errback_result)
            else:
                d.callback(self)

    def _heartbeat(self):
        if self._stopping:
            return
        if self._rejoin_needed:
            log.debug("%s: skipping heartbeat, rejoin needed", self)
            return
        if self._heartbeat_request_d:
            log.debug("%s: skipping heartbeat, in progress", self)
            return
        log.debug("%s: heartbeat", self)
        self._heartbeat_request_d = self.send_heartbeat_request()
        self._heartbeat_request_d.addCallbacks(
            self._handle_heartbeat_success,
            self._handle_heartbeat_failure,
        )

    def _handle_heartbeat_success(self, result):
        self._heartbeat_request_d = None
        log.debug("%s: heartbeat success", self)
        return result

    def _handle_heartbeat_failure(self, failure):
        self._heartbeat_request_d = None
        self._heartbeat_looper.stop()
        return self.rejoin_after_error(failure, label="heartbeat")

    def _heartbeat_timer_failed(self, failure):
        """
        Called when the heartbeat LoopingCall terminates with an unhandled
        exception. This represents a bug.
        """
        log.error(
            "%s Unhandled exception in heartbeat loop",
            self,
            exc_info=(failure.type, failure.value, failure.getTracebackObject()),
        )
        # TODO: This should handle the failure by leaving the group.
        return failure

    def _heartbeat_timer_stopped(self, result):
        self._heartbeat_looper_d = None
        return result

    def rejoin_after_error(self, result, label="rejoin_after_error"):
        rejoin_delay = self.retry_backoff_ms

        if result.check(RebalanceInProgress):
            log.debug("%s %s: group rebalance needed, rejoining", self, label)

        elif result.check(CoordinatorNotAvailable, NotCoordinatorForConsumerError):
            log.info("%s %s: group coordinator is invalid, rejoining", self, label)
            self.client.reset_consumer_group_metadata(self.group_id)

        elif result.check(IllegalGeneration):
            # we have been rejected - our consumers should not remain open
            log.info(
                "%s %s: generation id %s is not current, rejoining",
                self,
                label,
                self.generation_id,
            )
            self.on_group_leave()

        elif result.check(InvalidGroupId, UnknownMemberId):
            # we have been rejected - our consumers should not remain open
            log.info("%s %s: member id is not valid, rejoining", self, label)
            self.on_group_leave()
            self.member_id = ""

        elif result.check(InconsistentGroupProtocol):
            # this error can only happen if there's already a consumer group on
            # the topic and it doesn't support the protocols we've declared.
            # retrying is unlikely to help, so wait longer between attempts
            log.error("%s %s: group protocol was rejected, delaying rejoin", self, label)
            rejoin_delay = self.fatal_backoff_ms

        elif result.check(RequestTimedOutError):
            # If the request timed out, use the long delay before trying again
            log.warn("%s %s: request timed out, delaying rejoin", self, label)
            self.on_group_leave()
            self.client.reset_consumer_group_metadata(self.group_id)
            rejoin_delay = self.fatal_backoff_ms

        elif self._stopping and result.check(CancelledError):
            # Not really an error
            return

        elif result.check(KafkaError):
            # no other kafka errors are expected from the requests we're
            # sending out, so to get here means something is wrong.
            # retrying probably won't help, so wait longer between attempts
            log.error(
                "%s %s: unexpected response error: %r, delaying rejoin",
                self,
                label,
                result,
            )
            rejoin_delay = self.fatal_backoff_ms

        else:
            # if it wasn't a kafka error, we're not going to try to rejoin
            log.error("%s %s: unknown error %s", self, label, result)
            self.on_group_leave()
            self.stop(errback_result=result)
            return

        self._state = "[rejoin_needed]"
        self._rejoin_needed = True
        if not self._rejoin_wait_dc:
            rejoin_delay_s = rejoin_delay / 1000.0
            log.debug("%s %s: scheduling rejoin in %.1fs", self, label, rejoin_delay_s)
            self._rejoin_wait_dc = self.client.reactor.callLater(rejoin_delay_s, self.join_and_sync)

    def join_and_sync(self):
        """
        Called to join (or rejoin) the consumer group.
        Failures will result in join_and_sync being rescheduled
        to run again after a short delay, until it succeeds.

        Failed heartbeats will also reschedule join_and_sync
        """
        if self._rejoin_wait_dc:
            self._rejoin_wait_dc = None

        if not self._rejoin_needed:
            log.debug("join_and_sync: rejoin not needed")
            return

        # prevent multiple concurrent request situations
        if self._rejoin_d:
            # XXX: This should throw, not silently ignore.
            log.debug("join_and_sync: rejoin in progress")
            return

        def cleanup_rejoin_d(result):
            self._rejoin_d = None
            return result

        def rejoin_d_errback(result):
            log.error("%s error during join_and_sync: %s", self, result)

        self._rejoin_d = d = self._join_and_sync()
        d.addBoth(cleanup_rejoin_d).addErrback(rejoin_d_errback)
        return d

    @inlineCallbacks
    def _join_and_sync(self):
        self._state = "[fetching_broker]"
        coordinator_broker = yield self.get_coordinator_broker()
        if not coordinator_broker or self._stopping:
            return
        self.coordinator_broker = coordinator_broker

        self._state = "[joining]"
        yield self.on_join_prepare()
        join_response = yield self.send_join_group_request()
        if not join_response or self._stopping:
            # join failed, we'll be called again after a small delay
            return

        log.debug(
            "%s joined, leader=%s",
            self,
            join_response.leader_id == join_response.member_id,
        )
        assignments = []
        if join_response.leader_id == join_response.member_id:
            try:
                assignments = yield self.protocol.generate_assignments(
                    join_response.members,
                    topic_partitions={},
                )
            except _NeedTopicPartitions as e:
                topic_partitions = yield self.client._load_topic_partitions(*e.topics)
                assignments = yield self.protocol.generate_assignments(
                    join_response.members,
                    topic_partitions=topic_partitions,
                )

        self._state = "[syncing]"
        sync_response = yield self.send_sync_group_request(assignments)
        if not sync_response or self._stopping:
            # sync failed, we'll be called again after a small delay
            return

        # sync success - update our assignments
        # and restart the heartbeat timer
        assignment = self.protocol.decode_assignment(sync_response.member_assignment)
        self.reset_heartbeat_timer()
        self._rejoin_needed = False
        self._state = "[joined]"

        yield self.on_join_complete(assignment)

    def on_group_leave(self):
        """
        Called when the coordinator has been removed from the group
        (such as from a heartbeat error) and will not be rejoining
        """
        log.info("%s: on_group_leave", self)

    def on_join_prepare(self):
        """
        Called before joining or rejoining the group
        """
        log.info("%s: on_join_prepare", self)

    def on_join_complete(self, assignments):
        """
        Called after a successful group sync
        """
        log.info("%s: on_join_complete assignments=%r", self, assignments)


@attr.s(auto_exc=True)
class _NeedTopicPartitions(Exception):
    """
    More topic partition metadata is required

    The caller must load the partition IDs for these topics, then retry the
    operation.

    :ivar topics: Names of the required topics.
    :type topics: List[str]
    """

    topics = attr.ib()


class _ConsumerProtocol(object):
    """
    Implement the client-side assignment `Consumer Embedded Protocol`_

    This implementation is stateless and sans-I/O. It is rather naïve in that
    it doesn't rate-limit how fast rebalances occur.

    .. Consumer Embedded Protocol:
        https://cwiki.apache.org/confluence/display/KAFKA/Kafka+Client-side+Assignment+Proposal#KafkaClient-sideAssignmentProposal-ConsumerEmbeddedProtocol
    """

    protocol_type = "consumer"

    def join_group_protocols(self, topics):
        """
        Get a list of supported protocols.

        This implementation only supports the "consumer" protocol, version 0.

        :param topics:
            Topics to subscribe to.
        :type topics: List[str]

        :returns:
            Supported protocols, a single-element list.
        :rtype: List[_JoinGroupRequestProtocol]
        """
        metadata = KafkaCodec.encode_join_group_protocol_metadata(
            version=0,
            subscriptions=topics,
            user_data=b"",
        )
        return [_JoinGroupRequestProtocol(self.protocol_type, metadata)]

    def generate_assignments(self, members, topic_partitions):
        """
        Assign topic partitions to members.

        This is called on the leader once all group members have joined.

        :param members:
            Member join requests, as returned by
            :meth:`join_group_protocols()`. These requests encode the topics
            members are interested in.
        :type members: List[_JoinGroupResponseMember]

        :param topic_partitions: mapping of topic names to partition IDs
        :type topic_partitions: Mapping[str, List[int]]

        :returns: Member assignments.
        :rtype: List[_SyncGroupRequestMember]

        :raises _NeedTopicPartitions: when *topic_partitions* doesn't contain
            partitions for a topic one of the members has requested. The
            *topics* attribute indicates the topics that must be loaded for
            assignment to succeed.
        """
        member_metadata = {}
        for member in members:
            member_metadata[member.member_id] = KafkaCodec.decode_join_group_protocol_metadata(member.member_metadata)

        assignments = self._round_robin_assignment(member_metadata, topic_partitions)
        log.debug("%s: generate_assignments %r", self, assignments)

        encoded_assignments = []
        for member in members:
            encoded = KafkaCodec.encode_sync_group_member_assignment(
                version=0,
                assignments=assignments.get(member.member_id, {}),
                user_data=b"",
            )
            encoded_assignments.append(_SyncGroupRequestMember(member.member_id, encoded))
        return encoded_assignments

    def _round_robin_assignment(self, member_metadata, topic_partitions):
        # the algorithm for this is copied from kafka-python
        all_topics = set()
        for metadata in member_metadata.values():
            all_topics.update(metadata.subscriptions)

        assert all_topics

        try:
            all_topic_partitions = [(topic, partition) for topic in all_topics for partition in topic_partitions[topic]]
        except KeyError:
            raise _NeedTopicPartitions(all_topics)

        all_topic_partitions.sort()

        # construct {member_id: {topic: [partition, ...]}}
        assignment = collections.defaultdict(lambda: collections.defaultdict(list))

        member_iter = itertools.cycle(sorted(member_metadata.keys()))
        for topic, partition in all_topic_partitions:
            member_id = next(member_iter)

            # Because we constructed all_topic_partitions from the set of
            # member subscribed topics, we should be safe assuming that
            # each topic in all_topic_partitions is in at least one member
            # subscription; otherwise this could yield an infinite loop
            while topic not in member_metadata[member_id].subscriptions:
                member_id = next(member_iter)
            assignment[member_id][topic].append(partition)

        return assignment

    def decode_assignment(self, assignment):
        """
        Decode a topic partition assignment from the leader.

        :returns: Map of topic name to partition IDs.
        :rtype: Map[str, Tuple[int]]
        """
        assignment = KafkaCodec.decode_sync_group_member_assignment(assignment)
        log.debug("decode_assignment: assignment=%r", assignment)
        return assignment.assignments


class ConsumerGroup(Coordinator):
    """
    Coordinated consumer group implementation. Consuming the partitions
    on the topic(s) are load-balanced by Kafka among active connections.

    This is implemented by creating one :class:`afkak.Consumer` per
    topic partition. You can customise the behavior of these consumers via the
    *consumer_kwargs* parameter.

    :param client:
        The :class:`afkak.Client` to use.

    :param str group_id:
        Name of the consumer group to join for dynamic partition assignment,
        and to use for fetching and committing offsets.

    :param topics:
        Names of topics to consume. At least one topic must be given.
    :type topics: List[str]

    :param processor:
        Processing function for the consumers. See :class:`afkak.Consumer`.

    :param dict consumer_kwargs:
        Additional keyword arguments for the managed :class:`afkak.Consumer`\\ s

    :param float session_timeout_ms:
        The timeout used to detect failures when using Kafka's group
        management facilities.
        Default: 30000

    :param float heartbeat_interval_ms:
        The expected time in milliseconds between heartbeats to the consumer
        coordinator when using Kafka's group management feature. Heartbeats are
        used to ensure that the consumer's session stays active and to
        facilitate rebalancing when new consumers join or leave the group. The
        value must be set lower than session_timeout_ms, but typically should
        be set no higher than 1/3 of that value. It can be adjusted even lower
        to control the expected time for normal rebalances.

        Default: 5000

    :param float initial_backoff_ms:
        Milliseconds to backoff between attempts to find the group
        coordinator broker at startup.
        Default: 1000

    :param float retry_backoff_ms:
        Milliseconds to backoff when retrying from
        expected errors and group membership changes.
        Default: 100.

    :param float fatal_backoff_ms:
        Milliseconds to backoff when retrying on
        unexpected kafka errors
        Default: 10000.
    """

    def __init__(self, client, group_id, topics, processor, consumer_kwargs=None, **kwargs):
        super(ConsumerGroup, self).__init__(client, group_id, topics, **kwargs)
        self.processor = processor
        if not consumer_kwargs:
            consumer_kwargs = {}
        self.consumer_kwargs = consumer_kwargs
        self.consumers = {}

    def __repr__(self):
        return "<afkak.{} 0x{:x} for {!r} {} member_id={!r}>".format(
            self.__class__.__name__,
            id(self),
            self.group_id,
            self._state,
            self.member_id,
        )

    @inlineCallbacks
    def shutdown_consumers(self):
        """
        Shuts down all consumers (gracefully - they will commit and exit)
        """
        if self.consumers:
            current_consumers, self.consumers = self.consumers, {}
            num_consumers = sum(len(topic_consumers) for topic_consumers in current_consumers)
            shutdown_des = []
            for consumers in current_consumers.values():
                for consumer in consumers:
                    # if we take too long to commit, the server might
                    # reject us because it's already started the new generation
                    try:
                        shutdown_de = consumer.shutdown()
                    except Exception as e:
                        log.error("shutdown_consumers error in consumer %s: %s", consumer, e)
                        try:
                            # FIXME: This should not poke private state. It
                            # should store the deferred when it starts the
                            # consumer.
                            if consumer._start_d:
                                consumer.stop()
                        except Exception as e2:
                            log.error(
                                "shutdown_consumers stop error in consumer %s: %s",
                                consumer,
                                e2,
                            )
                    else:
                        shutdown_des.append(shutdown_de)
            try:
                yield DeferredList(shutdown_des, fireOnOneErrback=True, consumeErrors=True)
            except Exception as e:
                log.error("shutdown_consumers deferred error: %s", e)
                # try to kill all the consumers if graceful shutdown fails
                # and if that doesn't work, give up
                for consumers in current_consumers.values():
                    for consumer in consumers:
                        try:
                            if consumer._start_d:
                                consumer.stop()
                        except Exception as e2:
                            log.error("shutdown_consumers deferred stop error: %s", e2)
            log.debug("%s shutdown_consumers: %s consumers shutdown", self, num_consumers)

    def stop_consumers(self):
        """
        Shuts down all consumers (ungracefully)
        figure out when we want this
        """
        if self.consumers:
            current_consumers, self.consumers = self.consumers, {}
            num_consumers = sum(len(topic_consumers) for topic_consumers in current_consumers.values())
            for consumers in current_consumers.values():
                for consumer in consumers:
                    try:
                        if consumer._start_d:
                            consumer.stop()
                    except Exception as e2:
                        log.error(
                            "shutdown_consumers stop error in consumer %s: %s",
                            consumer,
                            e2,
                        )
            log.debug("stop_consumers (%s) %s consumers stopped", self, num_consumers)

    def on_group_leave(self):
        """
        Called when leaving the group forcefully
        all currently-held consumers will stop (forcibly)
        """
        log.debug("%s on_group_leave", self)
        self.stop_consumers()

    def on_join_prepare(self):
        """
        Called before joining or rejoining the group

        all currently-held partition consumers will commit and close
        """
        log.debug("%s on_join_prepare", self)
        return self.shutdown_consumers()

    def on_join_complete(self, assignments):
        """
        Called after a successful group sync

        starts up consumers for the newly assigned partitions
        """
        log.debug("%s on_join_complete: %s", self, assignments)
        for topic, partitions in assignments.items():
            for partition in partitions:
                consumer = Consumer(
                    client=self.client,
                    topic=topic,
                    partition=partition,
                    processor=self.processor,
                    consumer_group=self.group_id,
                    commit_consumer_id=self.member_id,
                    commit_generation_id=self.generation_id,
                    **self.consumer_kwargs,
                )
                self.consumers.setdefault(topic, []).append(consumer)
                start_d = consumer.start(OFFSET_COMMITTED)
                start_d.addErrback(self.on_consumer_error)
        log.info("consumergroup %s ready", self)

    def on_consumer_error(self, result):
        """
        errback for consumer's start_d, which gets called when the consumer
        gets an error in processing or committing that it can't handle.
        if the error is a type of error we know about (such as UnknownMemberID),
        we run our rejoin (restarting the consumer in the process), otherwise
        we will shutdown and raise it on our start() deferred.

        If the error was a CancelledError and we aren't managing any consumers,
        then the error came from stopping a consumer that we were shutting down
        and we can ignore it.
        """
        if result.check(CancelledError) and not self.consumers:
            log.info("on_consumer_error: %s", result)
            return

        self.rejoin_after_error(result, label="consumer_error")

    @inlineCallbacks
    def stop(self, errback_result=None):
        """
        Gracefully stop all consumers and depart the group.

        This waits for any ongoing processing to complete and commits offsets.
        It may take some time.
        """
        yield self.shutdown_consumers()
        yield super(ConsumerGroup, self).stop(errback_result=errback_result)
