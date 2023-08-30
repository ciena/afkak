Version 23.9.0+mcp0
==============

- **Feature:** Add support for record/recordBatch

- **Feature:** dynamic broker version detection

Version 21.5.0
==============

- **Feature:** Test with Python 3.9.

- **Removal:** Drop support for Python 2.7 and PyPy2, which are no longer supported by Twisted 21.2.0.

- **Bugfix:** `afkak.__version__` didn't match the package metadata. They now align ([#117](https://github.com/ciena/afkak/issues/117)).

Version 20.10.0
===============

- **Feature:** The ``ImportWarning`` ``Import of pyhash failed, using pure python`` is no longer issued at import time.
  Instead, an import warning with the text ``Using slow pure Python Murmur2 hash; install Afkak[FastMurmur2] for speedup`` is issued when the hash function is called by ``afkak.partitioner.HashedPartitioner``.
  This prevents the warning from appearing when using Afkak as a consumer.

Version 20.9.0
===============

- **Bugfix:** `afkak.Consumer` now respects *auto_offset_reset=OFFSET_LATEST* when no committed offset is found, rather than consuming the topic partition from the beginning.

Version 19.10.0
===============

- **Feature:** `afkak.KafkaClient` now accepts a sequence ``(host, port)`` tuples as its ``hosts`` argument and when passed to the ``update_cluster_hosts()`` method.
  This permits passing IPv6 addresses and fixes [#41](https://github.com/ciena/afkak/issues/41).

* **Feature:**  The string representation of `afkak.Consumer` instances has been cleaned up to make log messages that include it less noisy.
   It now looks like ``<Consumer topicname/0 running>`` where ``0`` is the partition number.

* **Feature:** Additional contextual information has been added to several of `afkak.Consumer`'s debug log messages.

* **Bugfix:** Afkak now depends on attrs ≥ 19.2.0 for an [auto_exc bugfix](https://github.com/python-attrs/attrs/issues/543) that affects an exception type internal to `afkak.ConsumerGroup`.
  Additionally, Afkak's test suite no longer generates `attr.s` deprecation warnings.

Version 19.8.0
==============

* **Feature:** The new `afkak.ConsumerGroup` class provides coordinated consumer support.
  This allows a group of processes to automatically distribute topic partitions among themselves.

  Fixes [#1](https://github.com/ciena/afkak/issues/1).

* **Feature:** The `Consumer` class has grown read-only attributes `last_offset_processed` and `last_offset_committed`.

* **Feature:** Debug log messages generating when sending requests have been enhanced to indicate the type of the request.

* **Feature:** The clientId has been removed from the string representation of internal broker objects.

* **Bugfix:** Afkak no longer logs a warning when a response is received for a request that was previously cancelled.
  When a truly unexpected response is received (e.g., due to a broker bug) an error is logged with up to 1024 bytes of the response.

* **Bugfix:** `afkak.consumer.Consumer._process_messages` has been modified to avoid using recursive callbacks that could result in a `RecursionError`.

  Fixes [#93](https://github.com/ciena/afkak/issues/93).

* **Feature:** The `KafkaClient.load_coordinator_for_group()` method replaces `KafkaClient.load_consumer_metadata_for_group()`.
  The latter method is now deprecated.

* **Backwards incompatible:** `KafkaClient.coordinator_fetches` has been renamed `_coordinator_fetches`, making it private.

* **Backwards incompatible:** `afkak.producer.Producer.sendLooper` and `.sendLooperD` are no longer public symbols.

Version 19.8.0b2
================

* The `Consumer` class has grown read-only attributes `last_offset_processed` and `last_offset_committed`.

* **Backwards incompatible:** The `Consumer.stop()` method and the deferreds returned by `Consumer.start()` and `Consumer.shutdown()` once again return the last processed offset, reverting a backwards-incompatible change in Afkak 19.6.0a1.

Version 19.8.0b1
================

* `afkak.consumer.Consumer._process_messages` has been modified to avoid using recursive callbacks that could result in a `RecursionError`.

  Fixes [#93](https://github.com/ciena/afkak/issues/93).

Version 19.6.0a1
================

* **Feature:** The new `afkak.ConsumerGroup` class provides coordinated consumer support.
  This allows a group of processes to automatically distribute topic partitions among themselves.

  Fixes [#1](https://github.com/ciena/afkak/issues/1).

* The `KafkaClient.load_consumer_metadata_for_group()` method has been deprecated.
  Its replacement is the `load_coordinator_for_group()` method which functions identically.

* **Backwards incompatible:** The `Consumer.stop()` method and the deferreds returned by `Consumer.start()` and `Consumer.shutdown()` now return a two-tuple of (last processed, last committed) offsets.
  Previously only the last processed offset was returned.

* **Backwards incompatible:** `KafkaClient.coordinator_fetches` has been renamed `_coordinator_fetches`, making it private.

* **Backwards incompatible:** `afkak.producer.Producer.sendLooper` and `.sendLooperD` are no longer public symbols.

* Debug log messages generating when sending requests have been enhanced to indicate the type of the request.

* The clientId has been removed from the string representation of internal broker objects.

* Afkak no longer logs a warning when a response is received for a request that was previously cancelled.
  When a truly unexpected response is received (e.g., due to a broker bug) an error is logged with up to 1024 bytes of the response.

Version 3.0.0
=============

Features
--------

* Compatibility with Python 3.5, 3.6, and 3.7.

  Afkak is now more strict about string types, even on Python 2.7.
  Topic and consumer group names are text — `str` on Python 3; `str` or `unicode` on Python 2.
  Message keys, content and offset commit metadata are bytes — `bytes` on Python 3; `str` on Python 2.

  The `FastMurmur2` extra now pulls in [pyhash](https://pypi.org/project/pyhash/) rather than [Murmur](https://pypi.org/project/Murmur/), as the former provides Python 3 support.

* The new ``snappy`` setuptools extra pulls in python-snappy, which is required for Snappy compression support.

* The `afkak.consumer.Consumer` class now supports an `auto_offset_reset` parameter.
  This controls how an `OffsetOutOfRange` error from the broker is handled.
  The default of `None` causes the error to propagate.

* Many internal changes have been made to enable the use of Twisted [endpoint APIs](https://twistedmatrix.com/documents/current/core/howto/endpoints.html) rather than `ReconnectingClientFactory`.
  The endpoint used to connect to the Kafka broker can be configured by passing the *endpoint_factory* argument to `KafkaClient`.
  The expotential backoff between connection attempts can now be configured by passing the *retry_policy* argument to `KafkaClient`.

* Afkak’s API documentation [is now available at afkak.readthedocs.io](https://afkak.readthedocs.io/en/latest/).

Bugfixes
--------

* Afkak now functions better when the network identity of Kafka brokers doesn’t match the network identity in cluster metadata.
  This could result in `KafkaUnavailableError` when using the client, rapid connection and disconnection, and frequent DNS resolution attempts.

  Fixes [#47](https://github.com/ciena/afkak/issues/47) and possibly [#15](https://github.com/ciena/afkak/issues/15).

* In a rare case when `afkak.consumer.Consumer` was stopped after all received messages have been processed and before invocation of an internal callback it would produce an `IndexError` with the message “list index out of range”.
  The consumer will now stop cleanly (fixes BPSO-94789).

* Afkak now raises a generic `afkak.common.BrokerResponseError` rather than ignoring unknown error codes.

Backwards-Incompatible Changes
------------------------------

This release includes many changes that are technically backwards-incompatible in that they change public API surface, but unlikely to impact real-world clients.

* The *timeout* argument to `KafkaClient` can no longer be set to `None` (meaning “no timeout”).
  You can still configure an outrageously large value.

  The value of *timeout* is now coerced with the `float()` function.
  It may raise `ValueError` in some circumstances where it used to raise `TypeError`.

* Keys passed to`afkak.partitioner.HashedPartitioner` must now be byte or text strings (`bytes` or `str` on Python 3; `str` or `unicode` on Python 2).

  Arbitrary objects are no longer stringified when passed as a partition key.
  Previously, unknown objects would be coerced by calling `str(key)`.
  Now a `TypeError` will be raised as this likely represents a programming error.

  `None` is no longer accepted as a partition key as not passing a key when using a hashed partitioner likely represents a programming error.
  Now a `TypeError` will be raised.
  Use `b''` as the partition key instead to get the same behavior `None` used to give.

* The way the reactor is passed around has been unified.
  `KafkaClient` now has a public `reactor` attribute which is used by `Producer` and `Consumer`.
  This change simplifies testing with mock I/O.

  The `clock` argument to `afkak.producer.Producer` has been removed.
  The producer now uses the reactor associated with the `KafkaClient` passed as its `client` argument.

  Fixes [#3](https://github.com/ciena/afkak/issues/3).

* The constants ``CODEC_NONE``, ``CODEC_GZIP``, and ``CODEC_SNAPPY`` have been relocated to the ``afkak.common`` module from the ``afkak.kafkacodec`` module.
  They remain importable directly from the ``afkak`` module.
  The ``ALL_CODECS`` constant is no longer available.

* Exception types for additional broker error codes have been added.
  These exceptions derive from `afkak.common.BrokerResponseError`.
  A subclass, `afkak.common.RetriableBrokerResponseError`, marks those error codes which are retriable.
  This information is also available as a bool on the `retriable` attribute.

  * The `message` attribute of these types has changed to match the upstream table:

    * `UnknownError`: `'UNKNOWN'` → `'UNKNOWN_SERVER_ERROR'`
    * `InvalidResponseError`: `'INVALID_MESSAGE'` → `'CORRUPT_MESSAGE'`
    * `StaleLeaderEpochCodeError`: `'STALE_LEADER_EPOCH_CODE'` → `'NETWORK_EXCEPTION'`
    * `OffsetsLoadInProgress`: `'OFFSETS_LOAD_IN_PROGRESS'` → `'COORDINATOR_LOAD_IN_PROGRESS'`
    * `ConsumerCoordinatorNotAvailableError`: `'CONSUMER_COORDINATOR_NOT_AVAILABLE'` → `'COORDINATOR_NOT_AVAILABLE'`

  * The following types have been renamed to match the name in the Kafka documentation, though the old names remain as deprecated aliases:
    <!-- TODO: actually deprecate them -->

    * `InvalidResponseError` → `CorruptMessage`
    * `StaleLeaderEpochCodeError` → `NetworkException`
    * `OffsetsLoadInProgress` → `CoordinatorLoadInProgress`
    * `ConsumerCoordinatorNotAvailableError` → `CoordinatorNotAvailable`
    * `NotCoordinatorForConsumerError` → `NotCoordinator`

* `afkak.common.kafka_errors` has been renamed to `afkak.common.BrokerResponseError.errnos`.

* A number of symbols have been made private or removed:

    * The meaning of `KafkaClient.clients` has changed and it is now documented as private.
      It will be renamed in a future release without further notice.
    * `KakaBrokerClient` has been renamed `_KafkaBrokerClient`, meaning it is no longer a public API.
    * The `afkak.protocol` module has been renamed `afkak._protocol`, meaning is no longer a public API.
    * The symbol `afkak.partitioner.murmur2_hash_c` no longer exists.
    * The `afkak.brokerclient.CLIENT_ID` constant has been removed.
    * `afkak.util` has been renamed `afkak._util`, meaning its contents are no longer part of the public API.
    * `afkak.common.check_error` has been renamed `_check_error`, making it private.

Version 2.9.0
=============

* Fix BPPF-4779: Kafka Producer could hang when KafkaClient improperly
  returned a generator from an internal method when a list was
  appropriate, side-stepping error handling. Previously, KafkaClient
  could return a generator which would produce a KeyError when
  evaluated, rather than a list of responses. Now the internal method
  `_send_broker_aware_request` returns a list of responses, rather
  than a generator. This seemed to be a rare occurance, seen only when
  a Kafka broker would return a response with only some of the
  requests returned.
* Fix BPPF-4856: When using the KafkaClient feature
  `disconnect_on_timeout` (introduced in Afkak 2.8.0),
  KafkaBrokerClient would leak a Twisted connector object each time
  `KafkaBrokerClient.disconnect` was called. This error has been
  fixed.
* Fix BPPF-3069: KafkaClient improperly handled 'None'
  timeout. Previously, if the KafkaClient were configured with a
  'None' timeout, when a `send_fetch_request` call was made, a
  TypeError would be raised.
* Various improvements to work toward Python-3 compatibility have been
  made.

Version 2.8.0
=============

* Fix BPPF-4438 by disconnecting on timeout when configured.
  Client: Add `disconnect_on_timeout` argument to `__init__`  which will
  allow caller to configure the KafkaClient to disconnect
  KafkaBrokerClients when requests via those brokers timeout.

Version 2.7.0
=============

* Consumer: Add shutdown() method which will gracefully stop the
  consumer after completing any ongoing processing and committing any
  offsets. (BPPF-416)
* Consumer: Fix issue where a TypeError would be thrown trying to
  commit a `None` offset when the consumer was started at the last
  committed offset and a commit was attempted before any messages had
  been processed. (BPPF-3752)

Version 2.6.0
=============

* KafkaBrokerClient: Fix for failing to reset its reconnect-delay on
  successful connection causing the first request after a connection
  drop to fail. (BPPF-330)
* KafkaBrokerClient: Added constructor argument to set initial
  reconnection delay and reduced from 2.72 secs to 0.272 secs.
* KafkaBrokerClient: Improved `__repr__` output
* KafkaBrokerClient: Added `connected` method which returns true if
  the KafkaBrokerClient currently has a valid connection to its
  broker.
* KafkaClient: Properly clear Consumer Group metadata in
  `reset_all_metadata` so that the out of date Consumer Group metadata
  will not be used when a Kafka broker goes down.
* KafkaClient: Fix leak of deferred (into deferredList) when closing
  KafkaBrokerClient due to metadata change. (BPSO-45921)
* KafkaClient: Refactor close of KafkaBrokerClients into separate
  method.
* KafkaClient: Use reactor-time when calculating blocked reactor, not
  wall-clock time.
* KafkaClient: Prefer connected brokers when sending a broker-agnostic
  request.
* KafkaConsumer: Fix typo in description of constructor argument.
* KafkaProtocol: Improve error message when receiving a message with a
  length specified as greater than the configured `MAX_LENGTH`.
* Test changes to cover the above function changes.
* Fix recursive infinte loop in `KafkaConsumer._handle_fetch_response`
  which could be triggered when closing the consumer.
* Fix `KafkaConsumer` so that it would not continue to make requests
  for more messages after `KafkaConsumer.stop()` call.

Version 2.5.0
=============

* Detect blocked reactor and log an Error
* allow produce 'null' message (delete tombstone)

Version 2.4.0
=============

* Actually fix `BPSO-10628`: Resolve hostnames to IPs for all
  configured hosts. Client will still return error on first failure,
  but will re-resolve before making further requests. High level
  Consumer and Producer retry.

Version 2.3.0
=============

* Resolve hostnames to IPs for all configured hosts. `BPSO-10628`.

Version 2.2.0
=============

* Add Kafka 0.9.0.1 compatibility. `BPSO-17091`.

Version 2.1.1
=============

* Switch to warnings.warn for failure to import native-code Murmur
  hash `BPSO-13212`.

Version 2.1.0
=============

* Fixed bug where Afkak would raise a KeyError when a commit failed
  `BPSO-11306`
* Added `request_retry_max_attempts` parameter to Consumer objects
  which allows the caller to configure the maximum number of attempts
  the Consumer will make on any request to Kafka. `BPSO-10531`
  NOTE: It defaults to zero which indicates the Consumer should retry
  forever. *This is a behavior change*.
* If a user-initiated Commit operation is attempted while a commit is
  ongoing (even a Consumer auto-initiated one), the new attempt will
  fail with a OperationInProgress error which contains a deferred
  which will fire when the previous Commit operation completes. This
  deferred *should* be used to retry the Commit since the ongoing
  operation may not include the latest offset at the time the second
  operation was initiated.
* Fixed an error where Commit requests would only be retried once
  before failing.
* Reduced the level of some log messages from ERROR to WARNING or
  lower. `BPSO-11309`
* Added `update_cluster_hosts` method to allow retargeting Afkak
  client in case all brokers are restarted on new IPs. `BPSO-3521`
* Fixed bug where Afkak would continue to try to contact brokers at IP
  addresses they no longer listened on, or brokers which had been
  removed from the cluster. `BPSO-6790`

Version 2.0.0
=============

* message processor callback will recieve Consumer object
  with which it was registered

Version 1.0.2
=============

* Fixed bug where message keys weren't sent to Kafka
* Fixed bug where producer didn't retry metadata lookup
* Fixed hashing of keys in HashedPartitioner to use Murmur2, like Java
* Shuffle the broker list when sending broker-unaware requests
* Reduced Twisted runtime requirement to 13.2.0
* Consolidated tox configuration to one tox.ini file
* Added logo
* Cleanup of License, ReadMe, Makefile, etc.

Version 1.0.1
=============

* Added Twisted as install requirement
* Readme augmented with better install instructions
* Handle testing properly without 'Snappy' installed

Version 1.0.0
=============

* Working offset committing on 0.8.2.1
* Full coverage tests
* Examples for using producer & consumer

Version 0.1.0
=============

* Large amount of rework of the base 'mumrah/kafka-python' to convert the APIs to async using Twisted
