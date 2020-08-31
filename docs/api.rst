API Documentation
=================

.. default-role:: any

.. module:: afkak

KafkaClient objects
~~~~~~~~~~~~~~~~~~~

.. autoclass:: afkak.KafkaClient
    :members:
    :undoc-members:

Consumer objects
~~~~~~~~~~~~~~~~

.. autoclass:: afkak.Consumer
    :members:
    :undoc-members:

ConsumerGroup objects
~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: afkak.ConsumerGroup

    .. automethod:: stop()


Offset constants
~~~~~~~~~~~~~~~~

.. data:: afkak.OFFSET_EARLIEST

.. data:: afkak.OFFSET_LATEST

.. data:: afkak.OFFSET_COMMITTED

Producer objects
~~~~~~~~~~~~~~~~

.. autoclass:: afkak.Producer
    :members:

Compression constants
~~~~~~~~~~~~~~~~~~~~~

.. data:: afkak.CODEC_NONE

    No compression.

.. data:: afkak.CODEC_GZIP

    Gzip compression.

.. data:: afkak.CODEC_SNAPPY

    Snappy compression.

    Snappy compression requires Afkak's ``snappy`` extra.
    For example::

        pip install afkak[snappy]

.. data:: afkak.CODEC_LZ4

    LZ4 compression. Not currently supported by Afkak.

Partitioners
~~~~~~~~~~~~

.. autoclass:: afkak.partitioners.RoundRobinPartitioner

.. autoclass:: afkak.partitioners.HashedPartitioner

Message construction
~~~~~~~~~~~~~~~~~~~~

Use these functions to construct payloads to send with `KafkaClient.send_produce_request()`.

.. autofunction:: afkak.create_message

.. autofunction:: afkak.create_message_set

Common objects
~~~~~~~~~~~~~~

.. TODO: Less terrible docs for this stuff.

.. automodule:: afkak.common
    :members:
    :undoc-members:
