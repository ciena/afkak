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

from typing import List

import attr
from twisted.internet.defer import Deferred

# Constants
DefaultKafkaPort = 9092
OFFSET_EARLIEST = -2  # From the docs for OffsetRequest
OFFSET_LATEST = -1  # From the docs for OffsetRequest
OFFSET_NOT_COMMITTED = -1  # Returned by kafka when no offset is stored
OFFSET_COMMITTED = -101  # Used to avoid possible additions from the Kafka team
TIMESTAMP_INVALID = -1  # Used to specify that the broker should set timestamp
KAFKA_SUCCESS = 0  # An 'error' of 0 is used to indicate success

PRODUCER_ACK_NOT_REQUIRED = 0  # No ack is required
PRODUCER_ACK_LOCAL_WRITE = 1  # Send response only after it is written to log
PRODUCER_ACK_ALL_REPLICAS = -1  # Response after data written by all replicas

CODEC_NONE = 0x00
CODEC_GZIP = 0x01
CODEC_SNAPPY = 0x02
CODEC_LZ4 = 0x03
# NB: This doesn't contain LZ4 because we don't support it yet.
_ALL_CODECS = (CODEC_NONE, CODEC_GZIP, CODEC_SNAPPY)

###############
#   Structs   #
###############


@attr.frozen(hash=False, eq=False)
class BaseStruct:
    """
    Base class for all structs.
    This class behaves like a tuple to maintain backwards compatibility
    """

    def __iter__(self):
        return iter(attr.astuple(self, recurse=False))

    def __getitem__(self, item):
        return attr.astuple(self)[item]

    def __len__(self):
        return len(attr.astuple(self))

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return attr.astuple(self) == attr.astuple(other)
        return NotImplemented

    def __hash__(self):
        return hash(attr.astuple(self))

    def __lt__(self, other):
        if isinstance(other, self.__class__):
            return attr.astuple(self) < attr.astuple(other)
        return NotImplemented


# SendRequest is used to encapsulate messages and keys prior to
# creating a message set
@attr.frozen
class SendRequest(BaseStruct):
    """
    Request to send a message to a topic

    Attributes:
        topic (str): The topic this request is for
        key (bytes): The key for the message
        messages (List[bytes]): The messages to include in the request
        deferred (Deferred): The deferred to fire when the request is complete
    """

    topic: str = attr.field()
    key: bytes = attr.field(default=None)
    messages: list = attr.field(default=None)
    deferred: Deferred = attr.field(default=None)


# Request payloads
@attr.frozen
class ProduceRequest(BaseStruct):
    """
    Request to produce a message to a topic/partition

    Attributes:
        topic (str): The topic this request is for
        partition (int): The partition this request is for
        messages (List[bytes]): The messages to include in the request
    """

    topic: str = attr.field()
    partition: int = attr.field()
    messages: List[bytes] = attr.field(default=None)


# Request payloads


@attr.frozen
class FetchRequest(BaseStruct):
    """
    Request to fetch a set of messages from a topic/partition

    Attributes:
        topic (str): The topic this request is for
        partition (int): The partition this request is for
        offset (int): The offset to begin this fetch from
        max_bytes (int): The maximum bytes to include in the message set for this partition
    """

    topic: str = attr.field()
    partition: int = attr.field()
    offset: int = attr.field()
    max_bytes: int = attr.field(default=None)


@attr.frozen
class OffsetRequest(BaseStruct):
    """
    Request to fetch the last committed offset for a set of partitions

    Attributes:
        topic (str): The topic this request is for
        partition (int): The partition this request is for
        time (int): The time to use for fetching offsets
        max_offsets (int): The maximum number of offsets to return
    """

    topic: str = attr.field()
    partition: int = attr.field()
    time: int = attr.field(default=None)
    max_offsets: int = attr.field(default=None)


@attr.frozen
class OffsetCommitRequest(BaseStruct):
    """
    Request to commit a specific offset for a topic/partition

    Attributes:
        topic (str): The topic this request is for
        partition (int): The partition this request is for
        offset (int): The offset to commit
        timestamp (int): The timestamp of the commit
        metadata (bytes): Any associated metadata the client wants to keep

    """

    topic: str = attr.field()
    partition: int = attr.field()
    offset: int = attr.field()
    timestamp: int = attr.field(default=None)
    metadata: bytes = attr.field(default=None)


@attr.frozen
class OffsetFetchRequest(BaseStruct):
    """
    Request to fetch the last committed offset for a set of partitions

    Attributes:
        topic (str): The topic this request is for
        partition (int): The partition this request is for

    """

    topic: str = attr.field()
    partition: int = attr.field()


# Response payloads
@attr.frozen
class ProduceResponse(BaseStruct):
    """
    Response from a ProduceRequest

    Attributes:
        topic (str): The topic this response entry is for
        partition (int): The partition this response entry is for
        error (int): The error code for this request
        offset (int): The offset of the message that was produced

    """

    topic: str = attr.field()
    partition: int = attr.field()
    error: int = attr.field()
    offset: int = attr.field()


@attr.frozen
class FetchResponse(BaseStruct):
    """
    Response from a FetchRequest

    Attributes:
        topic (str): The topic this response entry is for
        partition (int): The partition this response entry is for
        error (int): The error code for this request
        highwaterMark (int): The offset of the last message in the partition
        messages (list): The messages returned by the request

    """

    topic: str = attr.field()
    partition: int = attr.field()
    error: int = attr.field()
    highwaterMark: int = attr.field()
    messages: list = attr.field()


@attr.frozen
class OffsetResponse(BaseStruct):
    """
    Response from an OffsetRequest

    Attributes:
        topic (str): The topic this response entry is for
        partition (int): The partition this response entry is for
        error (int): The error code for this request
        offsets (list): The offsets returned by the request

    """

    topic: str = attr.field()
    partition: int = attr.field()
    error: int = attr.field()
    offsets: list = attr.field()


@attr.frozen
class OffsetCommitResponse(BaseStruct):
    """
    Response from an OffsetCommitRequest

    Attributes:
        topic (str): The topic this response entry is for
        partition (int): The partition this response entry is for
        error (int): The error code for this request

    """

    topic: str = attr.field()
    partition: int = attr.field()
    error: int = attr.field()


@attr.frozen
class OffsetFetchResponse(BaseStruct):
    """
    Response from an OffsetFetchRequest

    Attributes:
        topic (str): The topic this response entry is for
        partition (int): The partition this response entry is for
        offset (int): The offset for this partition
        metadata (bytes): Any associated metadata the server has for this partition
        error (int): The error code for this request

    """

    topic: str = attr.field()
    partition: int = attr.field()
    offset: int = attr.field()
    metadata: bytes = attr.field()
    error: int = attr.field()


@attr.frozen
class ConsumerMetadataResponse(BaseStruct):
    """
    Response from a ConsumerMetadataRequest

    Attributes:
        error (int): The error code for this request
        node_id (int): The node id for the coordinator
        host (str): The hostname of the coordinator
        port (int): The port for the coordinator

    """

    error: int = attr.field()
    node_id: int = attr.field()
    host: str = attr.field()
    port: int = attr.field()


# Metadata tuples
@attr.frozen
class BrokerMetadata(BaseStruct):
    """
    Metadata for a single broker

    Attributes:
        node_id (int): The node id for this broker
        host (str): The hostname of this broker
        port (int): The port for this broker

    """

    node_id: int = attr.field()
    host: str = attr.field()
    port: int = attr.field()


@attr.frozen
class TopicMetadata(BaseStruct):
    """
    Metadata for a single topic

    Attributes:
        topic (str): The topic name
        topic_error_code (int): The error code for this topic
        partition_metadata (list): The set of all partitions for this topic

    """

    topic: str = attr.field()
    topic_error_code: int = attr.field()
    partition_metadata: List[bytes] = attr.field()


@attr.frozen
class PartitionMetadata(BaseStruct):
    """
    Metadata for a single partition

    Attributes:
        topic (str): The topic name
        partition (int): The partition id
        partition_error_code (int): The error code for this partition
        leader (int): The node id for the leader
        replicas (list): The set of all nodes that host this partition
        isr (list): The set of nodes that are in sync with the leader for this partition

    """

    topic: str = attr.field()
    partition: int = attr.field()
    partition_error_code: int = attr.field()
    leader: int = attr.field()
    replicas: list = attr.field()
    isr: list = attr.field()


# ApiVersionRequest and ApiVersionResponse
@attr.frozen
class ApiVersionRequest(BaseStruct):
    """
    Request to discover supported API versions

    Attributes:
        api_key (int): The API key
        api_version (int): The API version

    """

    api_key: int = attr.field()
    api_version: int = attr.field()


@attr.frozen
class ApiVersionResponse(BaseStruct):
    """
    Response from an ApiVersionRequest

    Attributes:
        error_code (int): Response error code
        api_versions (list): List of supported API versions

    """

    error_code: int = attr.field()
    api_versions: list = attr.field()


# Requests and responses for consumer groups
@attr.s(frozen=True, slots=True)
class _JoinGroupRequestProtocol(object):
    """
    A protocol that the consumer supports.

    Attributes:
        protocol_name (str): The name of the protocol
        protocol_metadata (bytes): The encoded metadata for the protocol
    """

    protocol_name = attr.ib()
    protocol_metadata = attr.ib()


@attr.s(frozen=True, slots=True)
class _JoinGroupProtocolMetadata(object):
    """
    The metadata for a protocol that the consumer supports.

    Attributes:
        version (int): The version of the protocol
        subscriptions (list): The list of topics that the consumer wants to subscribe to
        user_data (bytes): Any additional data that the consumer wants to send to the group coordinator
    """

    version = attr.ib()
    subscriptions = attr.ib()
    user_data = attr.ib()


@attr.s(frozen=True, slots=True)
class _JoinGroupRequest(object):
    """
    A request to join a coordinator group.

    Attributes:
        group (str): The name of the group to join
        session_timeout (int): The time in milliseconds that the coordinator will wait for the consumer to send a
            JoinGroupResponse
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
        protocol_type (str): The type of protocol that the consumer supports
        group_protocols (list): The list of protocols that the consumer supports
    """

    group = attr.ib()
    session_timeout = attr.ib()
    member_id = attr.ib()
    protocol_type = attr.ib()
    group_protocols = attr.ib()


@attr.s(frozen=True, slots=True)
class _JoinGroupResponseMember(object):
    """
    A member of a consumer group.

    Attributes:
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
        member_metadata (bytes): The encoded metadata for the consumer
    """

    member_id = attr.ib()
    member_metadata = attr.ib()


@attr.s(frozen=True, slots=True)
class _JoinGroupResponse(object):
    """
    A response from a coordinator group.

    Attributes:
        error (int): The error code for the request
        generation_id (int): The generation id for the consumer group
        group_protocol (str): The protocol that the group coordinator selected
        leader_id (str): The unique identifier for the leader of the consumer group
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
        members (list): The list of members in the consumer group
    """

    error = attr.ib()
    generation_id = attr.ib()
    group_protocol = attr.ib()
    leader_id = attr.ib()
    member_id = attr.ib()
    members = attr.ib()


@attr.s(frozen=True, slots=True)
class _SyncGroupRequestMember(object):
    """
    A member of a consumer group.

    Attributes:
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
        member_metadata (bytes): The encoded metadata for the consumer
    """

    member_id = attr.ib()
    member_metadata = attr.ib()


@attr.s(frozen=True, slots=True)
class _SyncGroupMemberAssignment(object):
    """
    The assignment for a member of a consumer group.

    Attributes:
        version (int): The version of the assignment
        assignments (list): The list of partitions that the consumer is assigned to
        user_data (bytes): Any additional data that the consumer wants to send to the group coordinator
    """

    version = attr.ib()
    assignments = attr.ib()
    user_data = attr.ib()


@attr.s(frozen=True, slots=True)
class _SyncGroupRequest(object):
    """
    A request to sync with a consumer group.

    Attributes:
        group (str): The name of the group to sync with
        generation_id (int): The generation id for the consumer group
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
        group_assignment (dict): The assignment for each member of the consumer group
    """

    group = attr.ib()
    generation_id = attr.ib()
    member_id = attr.ib()
    group_assignment = attr.ib()


@attr.s(frozen=True, slots=True)
class _SyncGroupResponse(object):
    """
    A response from a consumer group sync.

    Attributes:
        error (int): The error code for the request
        member_assignment (bytes): The encoded assignment for the consumer
    """

    error = attr.ib()
    member_assignment = attr.ib()


@attr.s(frozen=True, slots=True)
class _HeartbeatRequest(BaseStruct):
    """
    A request to send a heartbeat to a consumer group coordinator.

    Attributes:
        group (str): The name of the group to send a heartbeat to
        generation_id (int): The generation id for the consumer group
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
    """

    group = attr.ib()
    generation_id = attr.ib()
    member_id = attr.ib()


@attr.s(frozen=True, slots=True)
class _HeartbeatResponse(BaseStruct):
    """
    A response from a consumer group heartbeat.

    Attributes:
        error (int): The error code for the request
    """

    error = attr.ib()


@attr.s(frozen=True, slots=True)
class _LeaveGroupRequest(BaseStruct):
    """
    A request to leave a consumer group.

    Attributes:
        group (str): The name of the group to leave
        member_id (str): The unique identifier for the consumer. This is generated by the consumer and should be
            unique across all consumers.
    """

    group = attr.ib()
    member_id = attr.ib()


@attr.s(frozen=True, slots=True)
class _LeaveGroupResponse(BaseStruct):
    """
    A response from a consumer group leave.

    Attributes:
        error (int): The error code for the request
    """

    error = attr.ib()


# Other useful structs
@attr.s(frozen=True, slots=True)
class OffsetAndMessage(BaseStruct):
    """
    A message with an associated offset.

    Attributes:
        offset (int): The offset of the message
        message (kafka.protocol.message.Message): The message
    """

    offset = attr.ib()
    message = attr.ib()


@attr.s(frozen=True, slots=True)
class TopicAndPartition(BaseStruct):
    """
    A topic and partition.

    Attributes:
        topic (str): The name of the topic
        partition (int): The partition id
    """

    topic = attr.ib()
    partition = attr.ib()


@attr.s(frozen=True, slots=True)
class SourcedMessage(BaseStruct):
    """
    A message with an associated offset, topic, and partition.

    Attributes:
        topic (str): The name of the topic
        partition (int): The partition id
        offset (int): The offset of the message
        message (kafka.protocol.message.Message): The message
    """

    topic = attr.ib()
    partition = attr.ib()
    offset = attr.ib()
    message = attr.ib()


@attr.s(frozen=True, slots=True, repr=False)
class Message(BaseStruct):
    """
    A Kafka `message`_ in format 0.

    :ivar int magic: Message format version, always 0.
    :ivar int attributes: Compression flags.
    :ivar bytes key:
        Message key, or ``None`` when the message lacks a key.
        Note that the key is required on a compacted topic.
    :ivar bytes value:
        Message value, or ``None`` if this is a tombstone a.k.a. null message.

    .. _message: https://kafka.apache.org/documentation/#messageset
    """

    magic = attr.field()
    attributes = attr.field()
    key = attr.field()
    value = attr.field()

    def __repr__(self):
        bits = ["<Message v0"]

        if self.attributes != 0:
            if self.attributes == CODEC_GZIP:
                codec = " CODEC_GZIP"
            elif self.attributes == CODEC_SNAPPY:
                codec = " CODEC_SNAPPY"
            elif self.attributes == CODEC_LZ4:
                codec = " CODEC_LZ4"
            else:
                codec = " attributes=0x{:x}".format(self.attributes)
            bits.append(codec)

        if self.key is not None:
            bits.append(" key={!r}".format(self.key))

        if self.value is None or len(self.value) < 1024:
            bits.append(" value={!r}".format(self.value))
        else:
            bits.append(" value={:,d} bytes {!r}...".format(len(self.value), self.value[:512]))

        bits.append(">")
        return "".join(bits)


#################
#   Exceptions  #
#################


class KafkaError(Exception):
    pass


class ClientError(KafkaError):
    """
    Generic error when the client detects an error
    """

    pass


class RestartError(ClientError):
    """
    Raised when a consumer start() call is made on an already running consumer
    """

    pass


class RestopError(ClientError):
    """
    Raised when a consumer stop() or shutdown() call is made on a
    non-running consumer
    """

    pass


class DuplicateRequestError(KafkaError):
    """
    Error caused by calling makeRequest() with a duplicate requestId
    """


class BrokerResponseError(KafkaError):
    """
    One `BrokerResponseError` subclass is defined for each protocol `error code`_.

    :ivar int errno:
        The integer error code reported by the server.

    :ivar bool retriable:
        A flag which indicates whether it is valid to retry the request which
        produced the error. Note that a metadata refresh may be required before
        retry, depending on the type of error.

    :ivar str message:
        The error code string, per the table. ``None`` if the error code is
        unknown to Afkak (future Kafka releases may add additional error
        codes). Note that this value may change for a given exception type.
        Code should either check the exception type or errno.

    .. _error code: https://kafka.apache.org/protocol.html#protocol_error_codes
    """

    retriable = False
    message = None

    def __str__(self):
        base = Exception.__str__(self)
        if self.message is None:
            return "error={:d} {}".format(self.errno, base)
        else:
            return "error={:d} ({}) {}".format(self.errno, self.message, base)

    @classmethod
    def raise_for_errno(cls, errno, *args):
        """
        Raise an exception for the given error number.

        :param int errno: Kafka error code.

        :raises BrokerResponseError:
            For any non-zero *errno* a `BrokerResponseError` is raised. If
            Afkak defines a specific exception type for the error code that is
            raised. All such types subclass `BrokerResponseError`.
        """
        if errno == 0:
            return None

        subcls = cls.errnos.get(errno)
        if subcls is None:
            error = cls(*args)
            error.errno = errno
            raise error
        else:
            raise subcls(*args)


class RetriableBrokerResponseError(BrokerResponseError):
    """
    `RetriableBrokerResponseError` is the shared superclass of all broker
    errors which can be retried.
    """

    retriable = True


class UnknownError(BrokerResponseError):
    errno = -1
    message = "UNKNOWN_SERVER_ERROR"


class OffsetOutOfRangeError(BrokerResponseError):
    errno = 1
    message = "OFFSET_OUT_OF_RANGE"


class CorruptMessage(RetriableBrokerResponseError):
    errno = 2
    message = "CORRUPT_MESSAGE"


# Compatibility alias:
InvalidMessageError = CorruptMessage


class UnknownTopicOrPartitionError(RetriableBrokerResponseError):
    errno = 3
    message = "UNKNOWN_TOPIC_OR_PARTITION"


class InvalidFetchRequestError(BrokerResponseError):
    errno = 4
    message = "INVALID_FETCH_SIZE"


class LeaderNotAvailableError(RetriableBrokerResponseError):
    errno = 5
    message = "LEADER_NOT_AVAILABLE"


class NotLeaderForPartitionError(RetriableBrokerResponseError):
    errno = 6
    message = "NOT_LEADER_FOR_PARTITION"


class RequestTimedOutError(RetriableBrokerResponseError):
    errno = 7
    message = "REQUEST_TIMED_OUT"


class BrokerNotAvailableError(BrokerResponseError):
    errno = 8
    message = "BROKER_NOT_AVAILABLE"


class ReplicaNotAvailableError(BrokerResponseError):
    errno = 9
    message = "REPLICA_NOT_AVAILABLE"


class MessageSizeTooLargeError(BrokerResponseError):
    errno = 10
    message = "MESSAGE_SIZE_TOO_LARGE"


class StaleControllerEpochError(BrokerResponseError):
    errno = 11
    message = "STALE_CONTROLLER_EPOCH"


class OffsetMetadataTooLargeError(BrokerResponseError):
    errno = 12
    message = "OFFSET_METADATA_TOO_LARGE"


class NetworkException(RetriableBrokerResponseError):
    errno = 13
    message = "NETWORK_EXCEPTION"


# Compatibility alias:
StaleLeaderEpochCodeError = NetworkException


class CoordinatorLoadInProgress(RetriableBrokerResponseError):
    errno = 14
    message = "COORDINATOR_LOAD_IN_PROGRESS"


# Compatibility alias:
OffsetsLoadInProgressError = CoordinatorLoadInProgress


class CoordinatorNotAvailable(RetriableBrokerResponseError):
    errno = 15
    message = "COORDINATOR_NOT_AVAILABLE"


# Compatibility alias:
ConsumerCoordinatorNotAvailableError = CoordinatorNotAvailable


class NotCoordinator(RetriableBrokerResponseError):
    errno = 16
    message = "NOT_COORDINATOR"


# Compatibility alias:
NotCoordinatorForConsumerError = NotCoordinator


class InvalidTopic(BrokerResponseError):
    """
    The request specified an illegal topic name. The name is either malformed
    or references an internal topic for which the operation is not valid.
    """

    errno = 17
    message = "INVALID_TOPIC_EXCEPTION"


class RecordListTooLarge(BrokerResponseError):
    """
    The produce request message batch exceeds the maximum configured segment
    size.
    """

    errno = 18
    message = "RECORD_LIST_TOO_LARGE"


class NotEnoughReplicas(RetriableBrokerResponseError):
    """
    The number of in-sync replicas is lower than can satisfy the number of acks
    required by the produce request.
    """

    errno = 19
    message = "NOT_ENOUGH_REPLICAS"


class NotEnoughReplicasAfterAppend(RetriableBrokerResponseError):
    """
    The produce request was written to the log, but not by as many in-sync
    replicas as it required.
    """

    errno = 20
    message = "NOT_ENOUGH_REPLICAS_AFTER_APPEND"


class InvalidRequiredAcks(BrokerResponseError):
    errno = 21
    message = "INVALID_REQUIRED_ACKS"


class IllegalGeneration(BrokerResponseError):
    errno = 22
    message = "ILLEGAL_GENERATION"


class InconsistentGroupProtocol(BrokerResponseError):
    errno = 23
    message = "INCONSISTENT_GROUP_PROTOCOL"


class InvalidGroupId(BrokerResponseError):
    errno = 24
    message = "INVALID_GROUP_ID"


class UnknownMemberId(BrokerResponseError):
    errno = 25
    message = "UNKNOWN_MEMBER_ID"


class InvalidSessionTimeout(BrokerResponseError):
    errno = 26
    message = "INVALID_SESSION_TIMEOUT"


class RebalanceInProgress(BrokerResponseError):
    errno = 27
    message = "REBALANCE_IN_PROGRESS"


class InvalidCommitOffsetSize(BrokerResponseError):
    errno = 28
    message = "INVALID_COMMIT_OFFSET_SIZE"


class TopicAuthorizationFailed(BrokerResponseError):
    errno = 29
    message = "TOPIC_AUTHORIZATION_FAILED"


class GroupAuthorizationFailed(BrokerResponseError):
    errno = 30
    message = "GROUP_AUTHORIZATION_FAILED"


class ClusterAuthorizationFailed(BrokerResponseError):
    errno = 31
    message = "CLUSTER_AUTHORIZATION_FAILED"


class InvalidTimestamp(BrokerResponseError):
    errno = 32
    message = "INVALID_TIMESTAMP"


class UnsupportedSaslMechanism(BrokerResponseError):
    errno = 33
    message = "UNSUPPORTED_SASL_MECHANISM"


class IllegalSaslState(BrokerResponseError):
    errno = 34
    message = "ILLEGAL_SASL_STATE"


class UnsupportedVersion(BrokerResponseError):
    errno = 35
    message = "UNSUPPORTED_VERSION"


class TopicAlreadyExists(BrokerResponseError):
    errno = 36
    message = "TOPIC_ALREADY_EXISTS"


class InvalidPartitions(BrokerResponseError):
    errno = 37
    message = "INVALID_PARTITIONS"


class InvalidReplicationFactor(BrokerResponseError):
    errno = 38
    message = "INVALID_REPLICATION_FACTOR"


class InvalidReplicaAssignment(BrokerResponseError):
    errno = 39
    message = "INVALID_REPLICA_ASSIGNMENT"


class InvalidConfig(BrokerResponseError):
    errno = 40
    message = "INVALID_CONFIG"


class NotController(RetriableBrokerResponseError):
    errno = 41
    message = "NOT_CONTROLLER"


class InvalidRequest(BrokerResponseError):
    errno = 42
    message = "INVALID_REQUEST"


class UnsupportedForMessageFormat(BrokerResponseError):
    errno = 43
    message = "UNSUPPORTED_FOR_MESSAGE_FORMAT"


class PolicyViolation(BrokerResponseError):
    errno = 44
    message = "POLICY_VIOLATION"


class OutOfOrderSequenceNumber(BrokerResponseError):
    errno = 45
    message = "OUT_OF_ORDER_SEQUENCE_NUMBER"


class DuplicateSequenceNumber(BrokerResponseError):
    errno = 46
    message = "DUPLICATE_SEQUENCE_NUMBER"


class InvalidProducerEpoch(BrokerResponseError):
    errno = 47
    message = "INVALID_PRODUCER_EPOCH"


class InvalidTxnState(BrokerResponseError):
    errno = 48
    message = "INVALID_TXN_STATE"


class InvalidProducerIdMapping(BrokerResponseError):
    errno = 49
    message = "INVALID_PRODUCER_ID_MAPPING"


class InvalidTransactionTimeout(BrokerResponseError):
    errno = 50
    message = "INVALID_TRANSACTION_TIMEOUT"


class ConcurrentTransactions(BrokerResponseError):
    errno = 51
    message = "CONCURRENT_TRANSACTIONS"


class TransactionCoordinatorFenced(BrokerResponseError):
    errno = 52
    message = "TRANSACTION_COORDINATOR_FENCED"


class TransactionalIdAuthorizationFailed(BrokerResponseError):
    errno = 53
    message = "TRANSACTIONAL_ID_AUTHORIZATION_FAILED"


class SecurityDisabled(BrokerResponseError):
    errno = 54
    message = "SECURITY_DISABLED"


class OperationNotAttempted(BrokerResponseError):
    errno = 55
    message = "OPERATION_NOT_ATTEMPTED"


class KafkaStorageError(RetriableBrokerResponseError):
    errno = 56
    message = "KAFKA_STORAGE_ERROR"


class LogDirNotFound(BrokerResponseError):
    errno = 57
    message = "LOG_DIR_NOT_FOUND"


class SaslAuthenticationFailed(BrokerResponseError):
    errno = 58
    message = "SASL_AUTHENTICATION_FAILED"


class UnknownProducerId(BrokerResponseError):
    errno = 59
    message = "UNKNOWN_PRODUCER_ID"


class ReassignmentInProgress(BrokerResponseError):
    errno = 60
    message = "REASSIGNMENT_IN_PROGRESS"


class DelegationTokenAuthDisabled(BrokerResponseError):
    errno = 61
    message = "DELEGATION_TOKEN_AUTH_DISABLED"


class DelegationTokenNotFound(BrokerResponseError):
    errno = 62
    message = "DELEGATION_TOKEN_NOT_FOUND"


class DelegationTokenOwnerMismatch(BrokerResponseError):
    errno = 63
    message = "DELEGATION_TOKEN_OWNER_MISMATCH"


class DelegationTokenRequestNotAllowed(BrokerResponseError):
    errno = 64
    message = "DELEGATION_TOKEN_REQUEST_NOT_ALLOWED"


class DelegationTokenAuthorizationFailed(BrokerResponseError):
    errno = 65
    message = "DELEGATION_TOKEN_AUTHORIZATION_FAILED"


class DelegationTokenExpired(BrokerResponseError):
    errno = 66
    message = "DELEGATION_TOKEN_EXPIRED"


class InvalidPrincipalType(BrokerResponseError):
    errno = 67
    message = "INVALID_PRINCIPAL_TYPE"


class NonEmptyGroup(BrokerResponseError):
    errno = 68
    message = "NON_EMPTY_GROUP"


class GroupIdNotFound(BrokerResponseError):
    errno = 69
    message = "GROUP_ID_NOT_FOUND"


class FetchSessionIdNotFound(RetriableBrokerResponseError):
    errno = 70
    message = "FETCH_SESSION_ID_NOT_FOUND"


class InvalidFetchSessionEpoch(RetriableBrokerResponseError):
    errno = 71
    message = "INVALID_FETCH_SESSION_EPOCH"


class ListenerNotFound(RetriableBrokerResponseError):
    errno = 72
    message = "LISTENER_NOT_FOUND"


class KafkaUnavailableError(KafkaError):
    pass


class LeaderUnavailableError(KafkaError):
    pass


class PartitionUnavailableError(KafkaError):
    pass


class FailedPayloadsError(KafkaError):
    """
    `FailedPayloadsError` indicates a partial or total failure

    In a method like `KafkaClient.send_produce_request()` partial failure is
    possible because payloads are distributed among the Kafka brokers that lead
    each partition.

    :ivar list responses: Any successful responses.
    :ivar list failed_payloads: Two-tuples of (payload, failure).
    """

    responses = property(lambda self: self.args[0])
    failed_payloads = property(lambda self: self.args[1])


class ConnectionError(KafkaError):
    pass


class BufferUnderflowError(KafkaError):
    pass


class ChecksumError(KafkaError):
    pass


class ConsumerFetchSizeTooSmall(KafkaError):
    pass


class ProtocolError(KafkaError):
    pass


class UnsupportedCodecError(KafkaError):
    pass


class CancelledError(KafkaError):
    def __init__(self, request_sent=None, message=None):
        self.request_sent = request_sent
        self.message = message

    def __str__(self):
        s = str(self.message) or "Cancelled"
        if self.request_sent is not None:
            s += " request_sent={!r}".format(self.request_sent)
        return s


class InvalidConsumerGroupError(KafkaError):
    pass


class NoResponseError(KafkaError):
    pass


class OperationInProgress(KafkaError):
    def __init__(self, deferred=None):
        """Create an OperationInProgress exception

        deferred is an optional argument which represents the operation
        currently in progress. It should fire when the current operation
        completes.
        """
        self.deferred = deferred


# TODO: document
BrokerResponseError.errnos = {
    -1: UnknownError,
    1: OffsetOutOfRangeError,
    2: CorruptMessage,
    3: UnknownTopicOrPartitionError,
    4: InvalidFetchRequestError,
    5: LeaderNotAvailableError,
    6: NotLeaderForPartitionError,
    7: RequestTimedOutError,
    8: BrokerNotAvailableError,
    9: ReplicaNotAvailableError,
    10: MessageSizeTooLargeError,
    11: StaleControllerEpochError,
    12: OffsetMetadataTooLargeError,
    13: NetworkException,
    14: CoordinatorLoadInProgress,
    15: CoordinatorNotAvailable,
    16: NotCoordinator,
    17: InvalidTopic,
    18: RecordListTooLarge,
    19: NotEnoughReplicas,
    20: NotEnoughReplicasAfterAppend,
    21: InvalidRequiredAcks,
    22: IllegalGeneration,
    23: InconsistentGroupProtocol,
    24: InvalidGroupId,
    25: UnknownMemberId,
    26: InvalidSessionTimeout,
    27: RebalanceInProgress,
    28: InvalidCommitOffsetSize,
    29: TopicAuthorizationFailed,
    30: GroupAuthorizationFailed,
    31: ClusterAuthorizationFailed,
    32: InvalidTimestamp,
    33: UnsupportedSaslMechanism,
    34: IllegalSaslState,
    35: UnsupportedVersion,
    36: TopicAlreadyExists,
    37: InvalidPartitions,
    38: InvalidReplicationFactor,
    39: InvalidReplicaAssignment,
    40: InvalidConfig,
    41: NotController,
    42: InvalidRequest,
    43: UnsupportedForMessageFormat,
    44: PolicyViolation,
    45: OutOfOrderSequenceNumber,
    46: DuplicateSequenceNumber,
    47: InvalidProducerEpoch,
    48: InvalidTxnState,
    49: InvalidProducerIdMapping,
    50: InvalidTransactionTimeout,
    51: ConcurrentTransactions,
    52: TransactionCoordinatorFenced,
    53: TransactionalIdAuthorizationFailed,
    54: SecurityDisabled,
    55: OperationNotAttempted,
    56: KafkaStorageError,
    57: LogDirNotFound,
    58: SaslAuthenticationFailed,
    59: UnknownProducerId,
    60: ReassignmentInProgress,
    61: DelegationTokenAuthDisabled,
    62: DelegationTokenNotFound,
    63: DelegationTokenOwnerMismatch,
    64: DelegationTokenRequestNotAllowed,
    65: DelegationTokenAuthorizationFailed,
    66: DelegationTokenExpired,
    67: InvalidPrincipalType,
    68: NonEmptyGroup,
    69: GroupIdNotFound,
    70: FetchSessionIdNotFound,
    71: InvalidFetchSessionEpoch,
    72: ListenerNotFound,
}


def _pretty_errno(errno):
    """
    Produce a string for a Kafka error code.

    The error code is looked up in the table and printed with its symbolic name:

    >>> _pretty_errno(5)
    5 (LEADER_NOT_AVAILABLE)

    The result is sensible when the code is 0:

    >>> _pretty_errno(0)
    '0 (no error)'

    It also copes with unknown error codes:

    >>> _pretty_errno(999)
    '999 (unknown error)'

    :param int errno: An error code from a Kafka PDU.

    :returns: string suitable for a log message
    """
    if errno == 0:
        message = "no error"
    else:
        cls = BrokerResponseError.errnos.get(errno)
        if cls is None:
            message = "unknown error"
        else:
            message = cls.message
    return "{:d} ({})".format(errno, message)
