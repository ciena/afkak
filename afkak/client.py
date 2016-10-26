# -*- coding: utf-8 -*-
# Copyright (C) 2015 Cyan, Inc.

"""KafkaClient class.

High level network client for an Apache Kafka Cluster.
"""
from __future__ import absolute_import

import logging
import random
import collections
from functools import partial
from twisted.names import client as DNSclient, dns
from twisted.internet.abstract import isIPAddress

from twisted.internet.defer import (
    inlineCallbacks, returnValue, DeferredList, succeed,
    CancelledError as t_CancelledError,
)

from .common import (
    TopicAndPartition, FailedPayloadsError, BrokerMetadata,
    PartitionUnavailableError, LeaderUnavailableError, KafkaUnavailableError,
    UnknownTopicOrPartitionError, NotLeaderForPartitionError, check_error,
    DefaultKafkaPort, RequestTimedOutError, KafkaError, kafka_errors,
    NotCoordinatorForConsumerError, OffsetsLoadInProgressError, UnknownError,
    ConsumerCoordinatorNotAvailableError, CancelledError,
)
from .kafkacodec import KafkaCodec
from .brokerclient import KafkaBrokerClient

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class KafkaClient(object):
    """Cluster-aware Kafka client.

    This is the high-level client which most clients should use. It maintains
    a collection of :class:`~afkak.KafkaBrokerClient` objects, one each to the
    various hosts in the Kafka cluster and auto selects the proper one based on
    the topic and partition of the request. It maintains a map of
    topics/partitions to brokers.

    A KafkaBrokerClient object maintains connections (reconnected as needed) to
    the various brokers.  It must be bootstrapped with at least one host to
    retrieve the cluster metadata.
    """

    # This is the __CLIENT_SIDE__ timeout that's used when making requests
    # to our brokerclients. If a request doesn't return within this amount
    # of time, we errback() the deferred. This is _NOT_ the server-side
    # timeout which is passed into the send_{produce,fetch}_request methods
    # which have defaults set below. This one should be larger, btw :-)
    DEFAULT_REQUEST_TIMEOUT_MSECS = 10000
    # Default timeout msec for fetch requests. This is how long the server
    # will wait trying to get enough bytes of messages to fulfill the fetch
    # request. When this times out on the server side, it sends back a
    # response with as many bytes of messages as it has. See the docs for
    # more caveats on this timeout.
    DEFAULT_FETCH_SERVER_WAIT_MSECS = 5000
    # Default minimum amount of message bytes sent back on a fetch request
    DEFAULT_FETCH_MIN_BYTES = 4096
    # Default number of msecs the lead-broker will wait for replics to
    # ack Produce requests before failing the request
    DEFAULT_REPLICAS_ACK_MSECS = 1000

    clientId = "afkak-client"

    def __init__(self, hosts, clientId=None,
                 timeout=DEFAULT_REQUEST_TIMEOUT_MSECS,
                 correlation_id=0,
                 reactor=None):

        if timeout is not None:
            timeout /= 1000.0  # msecs to secs
        self.timeout = timeout
        if clientId is not None:
            self.clientId = clientId

        # Setup all our initial attributes
        self.clients = {}  # (host,port) -> KafkaBrokerClient instance
        self.topics_to_brokers = {}  # TopicAndPartition -> BrokerMetadata
        self.partition_meta = {}  # TopicAndPartition -> PartitionMetadata
        self.consumer_group_to_brokers = {}  # consumer_group -> BrokerMetadata
        self.coordinator_fetches = {}  # consumer_group -> deferred
        self.topic_partitions = {}  # topic_id -> [0, 1, 2, ...]
        self.topic_errors = {}  # topic_id -> topic_error_code
        self.correlation_id = correlation_id
        self.load_metadata = None  # Deferred waiting on loading of metadata
        self.close_dlist = None  # Deferred wait on broker client disconnects
        self._brokers = {}  # Broker-NodeID -> BrokerMetadata
        self._topics = {}  # Topic-Name -> TopicMetadata
        self._closing = False  # Are we shutting down/shutdown?
        # clock/reactor for testing...
        self.clock = reactor

        # Store hosts for lookup later
        # Mark _collect_hosts_d as requiring lookup
        self._hosts = hosts
        self._collect_hosts_d = True

    def __repr__(self):
        """return a string representing this KafkaClient."""
        return '<KafkaClient clientId={0} brokers={1} timeout={2}>'.format(
            self.clientId, sorted(self.clients.keys()), self.timeout)

    def update_cluster_hosts(self, hosts):
        """Advise the Afkak client of possible changes to Kafka cluster hosts

        In general Afkak will keep up with changes to the cluster, but in
        a Docker environment where all the nodes in the cluster may change IP
        address at once or in quick succession Afkak may lose to
        all of the brokers.  This function lets you notify the Afkak client
        that some or all of the brokers may have changed. Afkak will compare
        the new list to the old and make new connections as needed.

        Parameters
        ==========
        hosts:
            (string|[string]) Hosts as a single comma separated
            "host[:port][,host[:port]]+" string, or a list of strings:
            ["host[:port]", ...]

        Return
        ======
        None
        """
        self._hosts = hosts
        self._collect_hosts_d = True

    def reset_topic_metadata(self, *topics):
        for topic in topics:
            try:
                partitions = self.topic_partitions[topic]
            except KeyError:
                continue

            for partition in partitions:
                self.topics_to_brokers.pop(
                    TopicAndPartition(topic, partition), None)

            del self.topic_partitions[topic]
            if topic in self.topic_errors:
                del self.topic_errors[topic]

    def reset_consumer_group_metadata(self, *groups):
        """Reset cache of what broker manages the offset for specified groups

        Remove the cache of what Kafka broker should be contacted when
        fetching or updating the committed offsets for a given consumer
        group or groups.

        NOTE: Does not cancel any outstanding requests for updates to the
        consumer group metadata for the specified groups.
        """
        for group in groups:
            if group in self.consumer_group_to_brokers:
                del self.consumer_group_to_brokers[group]

    def reset_all_metadata(self):
        self.topics_to_brokers.clear()
        self.topic_partitions.clear()
        self.topic_errors.clear()

    def has_metadata_for_topic(self, topic):
        return topic in self.topic_partitions

    def metadata_error_for_topic(self, topic):
        return self.topic_errors.get(
            topic, UnknownTopicOrPartitionError.errno)

    def close(self):
        # If we're already waiting on an/some outstanding disconnects
        # make sure we continue to wait for them...
        log.debug("%r: close", self)
        self._closing = True
        if not self.clients:
            # No clients to shutdown, just 'succeed'
            return succeed(None)
        if not self.close_dlist:
            dList = []
        else:
            log.debug("%r: close has nested deferredlist: %r",
                      self, self.close_dlist)
            dList = [self.close_dlist]
        for brokerClient in self.clients.values():
            log.debug("Calling close on broker client: %r", brokerClient)
            dList.append(brokerClient.close())
        log.debug("List of deferreds: %r", dList)
        self.close_dlist = DeferredList(dList)
        # clean up
        if self.load_metadata:
            d, self.load_metadata = self.load_metadata, None
            d.cancel()
        self.clients = {}
        self.reset_all_metadata()
        self.consumer_group_to_brokers.clear()
        return self.close_dlist

    def load_metadata_for_topics(self, *topics):
        """
        Discover brokers and metadata for a set of topics.
        This function is called lazily whenever metadata is unavailable.
        """
        log.debug("%r: load_metadata_for_topics: %r", self, topics)
        fetch_all_metadata = not topics
        # If we are already loading the metadata for all topics, then
        # just return the outstanding deferred
        if self.load_metadata and fetch_all_metadata:
            return self.load_metadata

        # create the request
        requestId = self._next_id()
        request = KafkaCodec.encode_metadata_request(
            self.clientId, requestId, topics)

        # Callbacks for the request deferred...
        def _handleMetadataResponse(response):
            # Decode the response
            (brokers, topics) = \
                KafkaCodec.decode_metadata_response(response)
            log.debug("%r: Broker/Topic metadata: %r/%r",
                      self, brokers, topics)

            # If we fetched the metadata for all topics, then store away the
            # received metadata for diagnostics.
            if fetch_all_metadata:
                self._brokers = brokers
                self._topics = topics

            # Iff we were fetching for all topics, and we got at least one
            # broker back, then remove brokers when we update our brokers
            ok_to_remove = (fetch_all_metadata and len(brokers))
            # Take the metadata we got back, update our self.clients, and
            # if needed disconnect or connect from/to old/new brokers
            self._update_brokers(
                [(b.host, b.port) for b in
                 brokers.itervalues()], remove=ok_to_remove)

            # Now loop through all the topics/partitions in the response
            # and setup our cache/data-structures
            for topic, topic_metadata in topics.items():
                _, topic_error, partitions = topic_metadata
                self.reset_topic_metadata(topic)
                self.topic_errors[topic] = topic_error
                if not partitions:
                    log.warning('No partitions for %s, Err:%d',
                                topic, topic_error)
                    continue

                self.topic_partitions[topic] = []
                for partition, meta in partitions.items():
                    self.topic_partitions[topic].append(partition)
                    topic_part = TopicAndPartition(topic, partition)
                    self.partition_meta[topic_part] = meta
                    if meta.leader == -1:
                        log.warning('No leader for topic %s partition %s',
                                    topic, partition)
                        self.topics_to_brokers[topic_part] = None
                    else:
                        self.topics_to_brokers[
                            topic_part] = brokers[meta.leader]
                self.topic_partitions[topic] = sorted(
                    self.topic_partitions[topic])
            return True

        def _handleMetadataErr(err):
            # This should maybe do more cleanup?
            if err.check(t_CancelledError, CancelledError):
                # Eat the error
                return None
            log.error("Failed to retrieve metadata:%s", err)
            raise KafkaUnavailableError(
                "Unable to load metadata from configured "
                "hosts: {!r}".format(err))

        def _clearLoadMetaD(resp):
            self.load_metadata = None
            return resp

        # Send the request, add the handlers
        d = self._send_broker_unaware_request(requestId, request)
        # If the request was for all topics, then save the deferred...
        if not topics:
            self.load_metadata = d
            self.load_metadata.addBoth(_clearLoadMetaD)
        d.addCallbacks(_handleMetadataResponse, _handleMetadataErr)
        return d

    def load_consumer_metadata_for_group(self, group):
        """Determine broker for the consumer metadata for the specified group

        Returns a deferred which callbacks with True if the group's coordinator
        could be determined, or errbacks with
        ConsumerCoordinatorNotAvailableError if not.
        """
        log.debug("%r: load_consumer_metadata_for_group: %r", self, group)

        # If we are already loading the metadata for this group, then
        # just return the outstanding deferred
        if group in self.coordinator_fetches:
            return self.coordinator_fetches[group]

        # No outstanding request, create a new one
        requestId = self._next_id()
        request = KafkaCodec.encode_consumermetadata_request(
            self.clientId, requestId, group)

        # Callbacks for the request deferred...
        def _handleConsumerMetadataResponse(response, group):
            # Clear the outstanding fetch
            self.coordinator_fetches.pop(group, None)
            # Decode the response (returns ConsumerMetadataResponse)
            c_m_resp = KafkaCodec.decode_consumermetadata_response(response)
            log.debug("%r: c_m_resp: %r", self, c_m_resp)
            if c_m_resp.error:
                # Raise the appropriate error
                resp_err = kafka_errors.get(
                    c_m_resp.error, UnknownError)(c_m_resp)
                raise resp_err

            self.consumer_group_to_brokers[group] = \
                BrokerMetadata(c_m_resp.node_id, c_m_resp.host,
                               c_m_resp.port)
            return True

        def _handleConsumerMetadataErr(err, group):
            # Clear the outstanding fetch
            self.coordinator_fetches.pop(group, None)
            log.error("Failed to retrieve consumer metadata "
                      "for group: %s Error:%r", group, err)
            # Clear any stored value for the group's coordinator
            self.reset_consumer_group_metadata(group)
            raise ConsumerCoordinatorNotAvailableError(
                "Coordinator for group: %s not available" % (group))

        # Send the request, add the handlers
        d = self._send_broker_unaware_request(requestId, request)
        # Save the deferred under the fetches for this group
        self.coordinator_fetches[group] = d
        d.addCallback(_handleConsumerMetadataResponse, group)
        d.addErrback(_handleConsumerMetadataErr, group)
        return d

    @inlineCallbacks
    def send_produce_request(self, payloads=None, acks=1,
                             timeout=DEFAULT_REPLICAS_ACK_MSECS,
                             fail_on_error=True, callback=None):
        """
        Encode and send some ProduceRequests

        ProduceRequests will be grouped by (topic, partition) and then
        sent to a specific broker. Output is a list of responses in the
        same order as the list of payloads specified

        Parameters
        ----------
        payloads:
            list of ProduceRequest
        acks:
            How many Kafka broker replicas need to write before
            the leader replies with a response
        timeout:
            How long the server has to receive the acks from the
            replicas before returning an error.
        fail_on_error:
            boolean, should we raise an Exception if we encounter an API error?
        callback:
            function, instead of returning the ProduceResponse,
            first pass it through this function

        Return
        ------
        a deferred which callbacks with a list of ProduceResponse

        Raises
        ------
        FailedPayloadsError, LeaderUnavailableError, PartitionUnavailableError
        """

        encoder = partial(
            KafkaCodec.encode_produce_request,
            acks=acks,
            timeout=timeout)

        if acks == 0:
            decoder = None
        else:
            decoder = KafkaCodec.decode_produce_response

        resps = yield self._send_broker_aware_request(
            payloads, encoder, decoder)

        returnValue(self._handle_responses(resps, fail_on_error, callback))

    @inlineCallbacks
    def send_fetch_request(self, payloads=None, fail_on_error=True,
                           callback=None,
                           max_wait_time=DEFAULT_FETCH_SERVER_WAIT_MSECS,
                           min_bytes=DEFAULT_FETCH_MIN_BYTES):
        """
        Encode and send a FetchRequest

        Payloads are grouped by topic and partition so they can be pipelined
        to the same brokers.

        Raises
        ======
        FailedPayloadsError, LeaderUnavailableError, PartitionUnavailableError
        """
        if (max_wait_time / 1000) > (self.timeout - 0.1):
            raise ValueError(
                "%r: max_wait_time: %d must be less than client.timeout by "
                "at least 100 milliseconds.", self, max_wait_time)

        encoder = partial(KafkaCodec.encode_fetch_request,
                          max_wait_time=max_wait_time,
                          min_bytes=min_bytes)

        # resps is a list of FetchResponse() objects, each of which can hold
        # 1-n messages.
        resps = yield self._send_broker_aware_request(
            payloads, encoder,
            KafkaCodec.decode_fetch_response)

        returnValue(self._handle_responses(resps, fail_on_error, callback))

    @inlineCallbacks
    def send_offset_request(self, payloads=None, fail_on_error=True,
                            callback=None):
        resps = yield self._send_broker_aware_request(
            payloads,
            KafkaCodec.encode_offset_request,
            KafkaCodec.decode_offset_response)

        returnValue(self._handle_responses(resps, fail_on_error, callback))

    @inlineCallbacks
    def send_offset_fetch_request(self, group, payloads=None,
                                  fail_on_error=True, callback=None):
        """
        Takes a group (string) and list of OffsetFetchRequest and returns
        a list of OffsetFetchResponse objects
        """
        encoder = partial(KafkaCodec.encode_offset_fetch_request,
                          group=group)
        decoder = KafkaCodec.decode_offset_fetch_response
        resps = yield self._send_broker_aware_request(
            payloads, encoder, decoder, consumer_group=group)

        returnValue(self._handle_responses(
            resps, fail_on_error, callback, group))

    @inlineCallbacks
    def send_offset_commit_request(self, group, payloads=None,
                                   fail_on_error=True, callback=None,
                                   group_generation_id=-1,
                                   consumer_id=''):
        """Send a list of OffsetCommitRequests to the Kafka broker for the
        given consumer group.

        Args:
          group (str): The consumer group to which to commit the offsets
          payloads ([OffsetCommitRequest]): List of topic, partition, offsets
            to commit.
          fail_on_error (bool): Whether to raise an exception if a response
            from the Kafka broker indicates an error
          callback (callable): a function to call with each of the responses
            before returning the returned value to the caller.
          group_generation_id (int): Must currently always be -1
          consumer_id (str): Must currently always be empty string
        Returns:
          [OffsetCommitResponse]: List of OffsetCommitResponse objects.
          Will raise KafkaError for failed requests if fail_on_error is True
        """
        encoder = partial(KafkaCodec.encode_offset_commit_request,
                          group=group, group_generation_id=group_generation_id,
                          consumer_id=consumer_id)
        decoder = KafkaCodec.decode_offset_commit_response
        resps = yield self._send_broker_aware_request(
            payloads, encoder, decoder, consumer_group=group)

        returnValue(self._handle_responses(
            resps, fail_on_error, callback, group))

    # # # Private Methods # # #

    def _handle_responses(self, responses, fail_on_error, callback=None,
                          consumer_group=None):
        out = []
        for resp in responses:
            try:
                check_error(resp)
            except (UnknownTopicOrPartitionError, NotLeaderForPartitionError):
                log.exception('Error found in response: %s', resp)
                self.reset_topic_metadata(resp.topic)
                if fail_on_error:
                    raise
            except (OffsetsLoadInProgressError,
                    NotCoordinatorForConsumerError,
                    ConsumerCoordinatorNotAvailableError):
                log.exception('Error found in response: %s Consumer Group: %s',
                              resp, consumer_group)
                self.reset_consumer_group_metadata(consumer_group)
                if fail_on_error:
                    raise

            if callback is not None:
                out.append(callback(resp))
            else:
                out.append(resp)
        return out

    def _get_clock(self):
        # Reactor to use for connecting, callLater, etc [test]
        if self.clock is None:
            from twisted.internet import reactor
            self.clock = reactor
        return self.clock

    def _get_brokerclient(self, host, port):
        """
        Get or create a connection to a broker using host and port.
        Returns the broker immediately, but the broker may have just been
        created and be in an unconnected state. The broker will connect on
        an as-needed basis when processing a request.
        """
        host_key = (host, port)
        if host_key not in self.clients:
            # We don't have a brokerclient for that host/port, create one,
            # ask it to connect
            log.debug("%r: creating new KafkaBrokerClient: %r", self,
                      host_key)
            self.clients[host_key] = KafkaBrokerClient(
                host, port, clientId=self.clientId,
                subscribers=[self._update_broker_state],
                )
        return self.clients[host_key]

    def _update_broker_state(self, broker, connected, reason):
        """
        Handle updates of a broker's connection state.  If we get an update
        with a state other than 'connected', reset our metadata, as it
        indicates that a connection to one of our brokers ended, or failed to
        come up correctly
        """
        state = "Connected" if connected else "Disconnected"
        log.debug(
            "Broker:%r state changed:%s for reason:%r", broker, state, reason)
        # If one of our broker clients disconnected, there may be a metadata
        # change. Make sure we check...
        if not connected:
            self.reset_all_metadata()
            if not self._closing:
                if self._collect_hosts_d is None:
                    self._collect_hosts_d = True
                e = self.load_metadata_for_topics()
                e.addErrback(lambda fail: log.error(
                    "Failure: %r loading metadata", fail))

    def _update_brokers(self, new_brokers, remove=False):
        """ Update our self.clients based on brokers in received metadata
        Take the received dict of brokers and reconcile it with our current
        list of brokers (self.clients). If there is a new one, bring up a new
        connection to it, and if remove is True, and any in our current list
        aren't in the metadata returned, disconnect from it.
        """
        log.debug("%r: _update_brokers: %r remove: %r",
                  self, new_brokers, remove)

        # Work with the brokers as sets
        new_brokers = set(new_brokers)
        current_brokers = set(self.clients.keys())

        # set of added
        added_brokers = new_brokers - current_brokers
        # removed
        removed_brokers = current_brokers - new_brokers

        # Create any new brokers based on the new metadata
        for broker in added_brokers:
            self._get_brokerclient(*broker)

        # Disconnect and remove from self.clients any removed
        if remove and removed_brokers:
            if not self.close_dlist:
                dList = []
            else:
                log.debug("%r: _update_brokers has nested deferredlist: %r",
                          self, self.close_dlist)
                dList = [self.close_dlist]
            for broker in removed_brokers:
                # broker better be in self.clients if not, weirdness
                brokerClient = self.clients.pop(broker)
                log.debug("Calling close on: %r", brokerClient)
                dList.append(brokerClient.close())
            self.close_dlist = DeferredList(dList)

    @inlineCallbacks
    def _get_leader_for_partition(self, topic, partition):
        """
        Returns the leader for a partition or None if the partition exists
        but has no leader.

        PartitionUnavailableError will be raised if the topic or partition
        is not part of the metadata.
        """

        key = TopicAndPartition(topic, partition)
        # reload metadata whether the partition is not available
        # or has no leader (broker is None)
        if self.topics_to_brokers.get(key) is None:
            yield self.load_metadata_for_topics(topic)

        if key not in self.topics_to_brokers:
            raise PartitionUnavailableError("%s not available" % str(key))

        returnValue(self.topics_to_brokers[key])

    @inlineCallbacks
    def _get_coordinator_for_group(self, consumer_group):
        """Returns the coordinator (broker) for a consumer group

        Returns the broker for a given consumer group or
        Raises ConsumerCoordinatorNotAvailableError
        """
        if self.consumer_group_to_brokers.get(consumer_group) is None:
            yield self.load_consumer_metadata_for_group(consumer_group)

        returnValue(self.consumer_group_to_brokers.get(consumer_group))

    def _next_id(self):
        """Generate a new correlation id."""
        # modulo to keep within int32 (signed)
        self.correlation_id = (self.correlation_id + 1) % 2**31
        return self.correlation_id

    def _make_request_to_broker(self, broker, requestId, request, **kwArgs):
        """Send a request to the specified broker."""
        def _timeout_request(broker, requestId):
            """The time we allotted for the request expired, cancel it."""
            try:
                broker.cancelRequest(requestId, reason=RequestTimedOutError(
                    'Request: {} cancelled due to timeout'.format(requestId)))
            except KeyError:  # pragma: no cover This should never happen...
                log.exception('ERROR: Failed to find key for timed-out '
                              'request. Broker: %r Req: %d',
                              broker, requestId)
                raise

        def _cancel_timeout(_, dc):
            """Request completed/cancelled, cancel the timeout delayedCall."""
            if dc.active():
                dc.cancel()
            return _

        # Make the request to the specified broker
        d = broker.makeRequest(requestId, request, **kwArgs)
        if self.timeout is not None:
            # Set a delayedCall to fire if we don't get a reply in time
            dc = self._get_clock().callLater(
                self.timeout, _timeout_request, broker, requestId)
            # Setup a callback on the request deferred to cancel timeout
            d.addBoth(_cancel_timeout, dc)
        return d

    @inlineCallbacks
    def _send_broker_unaware_request(self, requestId, request, brokers=None):
        """
        Attempt to send a broker-agnostic request to one of the available
        brokers. Keep trying until you succeed, or run out of hosts to try
        """

        if self._collect_hosts_d is True:
            self._collect_hosts_d = _collect_hosts(self._hosts)
            hosts = yield self._collect_hosts_d
            self._clear_collect_hosts()
            self._update_brokers(hosts, remove=True)
        elif self._collect_hosts_d is not None:
            yield _collect_hosts(self._hosts)

        if brokers is None:
            brokers = self.clients.values()[:]
            random.shuffle(brokers)
        for broker in brokers:
            try:
                log.debug('_sbur: sending request: %d to broker: %r',
                          requestId, broker)
                d = self._make_request_to_broker(broker, requestId, request)
                resp = yield d
                returnValue(resp)
            except KafkaError as e:
                log.warning("Could not makeRequest [%r] to server %s:%i, "
                            "trying next server. Err: %r",
                            request, broker.host, broker.port, e)

        raise KafkaUnavailableError(
            "All servers [%r] failed to process request" % self.clients.keys())

    @inlineCallbacks
    def _send_broker_aware_request(self, payloads, encoder_fn, decode_fn,
                                   consumer_group=None):
        """
        Group a list of request payloads by topic+partition and send them to
        the leader broker for that partition using the supplied encode/decode
        functions

        Params
        ======
        payloads: list of object-like entities with a topic and
                  partition attribute. payloads must be grouped by
                  (topic, partition) tuples.
        encode_fn: a method to encode the list of payloads to a request body,
                   must accept client_id, correlation_id, and payloads as
                   keyword arguments
        decode_fn: a method to decode a response body into response objects.
                   The response objects must be object-like and have topic
                   and partition attributes
        consumer_group: [string], optional. Indicates the request should be
                   directed to the Offset Coordinator for the specified
                   consumer_group.

        Return
        ======
        deferred yielding a generator of response objects in the same order
        as the supplied payloads

        Raises
        ======
        FailedPayloadsError, LeaderUnavailableError, PartitionUnavailableError,

        """

        # Calling this without payloads is nonsensical
        if not payloads:
            raise ValueError("Payloads parameter is empty")

        # Group the requests by topic+partition
        original_keys = []
        payloads_by_broker = collections.defaultdict(list)

        # Go through all the payloads, lookup the leader/coordinator for that
        # payload's topic/partition or consumer group. If there's no
        # leader/coordinator (broker), raise. For each broker, keep
        # a list of the payloads to be sent to it. Also, for each payload in
        # the list of payloads, make a corresponding list (original_keys) with
        # the topic/partition in the same order, so we can lookup the returned
        # result(s) by that topic/partition key in the set of returned results
        # and return them in a list the same order the payloads were supplied
        for payload in payloads:
            # get leader/coordinator, depending on consumer_group
            if consumer_group is None:
                leader = yield self._get_leader_for_partition(
                    payload.topic, payload.partition)
                if leader is None:
                    raise LeaderUnavailableError(
                        "Leader not available for topic %s partition %s" %
                        (payload.topic, payload.partition))
            else:
                leader = yield self._get_coordinator_for_group(consumer_group)
                if leader is None:
                    raise ConsumerCoordinatorNotAvailableError(
                        "Coordinator not available for group: %s" %
                        (consumer_group))

            payloads_by_broker[leader].append(payload)
            original_keys.append((payload.topic, payload.partition))

        # Accumulate the responses in a dictionary
        acc = {}

        # The kafka server doesn't send replies to produce requests
        # with acks=0. In that case, our decode_fn will be
        # None, and we need to let the brokerclient know not
        # to expect a reply. makeRequest() returns a deferred
        # regardless, but in the expectResponse=False case, it will
        # fire as soon as the request is sent, and it can errBack()
        # due to being cancelled prior to the broker being able to
        # send the request.
        expectResponse = decode_fn is not None

        # keep a list of payloads that were failed to be sent to brokers
        failed_payloads = []

        # Keep track of outstanding requests in a list of deferreds
        inFlight = []
        # and the payloads that go along with them
        payloadsList = []
        # For each broker, send the list of request payloads,
        for broker_meta, payloads in payloads_by_broker.items():
            broker = self._get_brokerclient(broker_meta.host, broker_meta.port)
            requestId = self._next_id()
            request = encoder_fn(client_id=self.clientId,
                                 correlation_id=requestId, payloads=payloads)

            # Make the request
            d = self._make_request_to_broker(broker, requestId, request,
                                             expectResponse=expectResponse)
            inFlight.append(d)
            payloadsList.append(payloads)

        # Wait for all the responses to come back, or the requests to fail
        results = yield DeferredList(inFlight, consumeErrors=True)
        # We now have a list of (succeeded, response/Failure) tuples. Check 'em
        for (success, response), payloads in zip(results, payloadsList):
            if not success:
                # The brokerclient deferred was errback()'d:
                #   The send failed, or this request was cancelled (by timeout)
                log.debug("%r: request:%r to broker failed: %r", self,
                          payloads, response)
                failed_payloads.extend([(p, response) for p in payloads])
                continue
            if not expectResponse:
                continue
            # Successful request/response. Decode it
            for response in decode_fn(response):
                acc[(response.topic, response.partition)] = response

        # Order the accumulated responses by the original key order
        # Note that this scheme will throw away responses which we did
        # not request.  See test_send_fetch_request, where the response
        # includes an error, but for a topic/part we didn't request.
        # Since that topic/partition isn't in original_keys, we don't pass
        # it back from here and it doesn't error out.
        # If any of the payloads failed, fail
        responses = (acc[k] for k in original_keys) if acc else ()
        if failed_payloads:
            self.reset_all_metadata()
            raise FailedPayloadsError(responses, failed_payloads)

        returnValue(responses)

    def _clear_collect_hosts(self):
        self._collect_hosts_d = None


@inlineCallbacks
def _collect_hosts(hosts):
    """Turn hosts args into a list of tuples
    Takes a list of string or a string with comma separated entries
    of the form <host>:<port> or <host> and returns a list of
    (<host>, <port>) tuples """
    if isinstance(hosts, basestring):
        hosts = hosts.strip().split(',')
    result = set()
    for host_port in hosts:
        res = host_port.split(':')
        host = res[0]
        port = int(res[1]) if len(res) > 1 else DefaultKafkaPort

        IP_addresses = yield _get_IP_addresses(host)
        if IP_addresses is None:
            continue
        result |= set(_make_IPHost_tuples(IP_addresses, port))
    returnValue(list(result))


@inlineCallbacks
def _get_IP_addresses(host_address):
    """
    Resolves an an address/URL to a list of IPv4 addresses
    """
    if isIPAddress(host_address):
        returnValue([host_address])
    else:
        answers, auth, addit = yield DNSclient.lookupAddress(host_address)
        if answers:
            returnValue(
                [
                    answer.payload.dottedQuad()
                    for answer in answers
                    if answer.type == dns.A
                ],
            )
        else:
            returnValue(None)


def _make_IPHost_tuples(IP_addresses, port):
    """
    Given a list of IP addresses, creates a list of 2-tuples for which each
    2-tuple is a (IP address, port)
    """
    return [(address, port) for address in IP_addresses]
