# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2016, 2017, 2018 Ciena Corporation

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO

import gzip
import struct

# A header which indicates that the data was encoded with the blocking mode
# of the xerial snappy library. ("Blocking" in this context means "split
# into length-prefixed blocks".)
#
# This mode writes a magic header of the format:
#
#     +--------+--------------+------------+---------+--------+
#     | Marker | Magic String | Null / Pad | Version | Compat |
#     |--------+--------------+------------+---------+--------|
#     |  byte  |   c-string   |    byte    |  int32  | int32  |
#     |--------+--------------+------------+---------+--------|
#     |  -126  |   'SNAPPY'   |     \0     |         |        |
#     +--------+--------------+------------+---------+--------+
#
# The pad appears to be to ensure that SNAPPY is a valid cstring
# The version is the version of this format as written by xerial,
# in the wild this is currently 1 as such we only support v1.
#
# Compat is there to claim the minimum supported version that
# can read a xerial block stream, presently in the wild this is 1.
#
# See https://github.com/dpkp/kafka-python/pull/127 for background.
_XERIAL_HEADER = b"\x82SNAPPY\x00\x00\x00\x00\x01\x00\x00\x00\x01"

try:
    import snappy

    _has_snappy = True
except ImportError:
    _has_snappy = False


def has_gzip():
    return True


def has_snappy():
    return _has_snappy


def gzip_encode(payload):
    buffer = BytesIO()
    handle = gzip.GzipFile(fileobj=buffer, mode="w")
    handle.write(payload)
    handle.close()
    return buffer.getvalue()


def gzip_decode(payload):
    buffer = BytesIO(payload)
    handle = gzip.GzipFile(fileobj=buffer, mode="r")
    result = handle.read()
    handle.close()
    buffer.close()
    return result


def snappy_encode(payload, xerial_compatible=False, xerial_blocksize=32 * 1024):
    """
    Compress the given data with the Snappy algorithm.

    :param bytes payload: Data to compress.
    :param bool xerial_compatible:
        If set then the stream is broken into length-prefixed blocks in
        a fashion compatible with the xerial snappy library.

        The format winds up being::

            +-------------+------------+--------------+------------+--------------+
            |   Header    | Block1_len | Block1 data  | BlockN len | BlockN data  |
            |-------------+------------+--------------+------------+--------------|
            |  16 bytes   |  BE int32  | snappy bytes |  BE int32  | snappy bytes |
            +-------------+------------+--------------+------------+--------------+

    :param int xerial_blocksize:
        Number of bytes per chunk to independently Snappy encode. 32k is the
        default in the xerial library.

    :returns: Compressed bytes.
    :rtype: :class:`bytes`
    """
    if not has_snappy():  # FIXME This should be static, not checked every call.
        raise NotImplementedError("Snappy codec is not available")

    if xerial_compatible:

        def _chunker():
            for i in range(0, len(payload), xerial_blocksize):
                yield payload[i : i + xerial_blocksize]

        out = BytesIO()
        out.write(_XERIAL_HEADER)

        for chunk in _chunker():
            block = snappy.compress(chunk)
            out.write(struct.pack("!i", len(block)))
            out.write(block)

        out.seek(0)
        return out.read()

    else:
        return snappy.compress(payload)


def snappy_decode(payload):
    if not has_snappy():
        raise NotImplementedError("Snappy codec is not available")

    if payload.startswith(_XERIAL_HEADER):
        # TODO ? Should become a fileobj ?
        view = memoryview(payload)
        out = []
        length = len(payload)

        cursor = 16
        while cursor < length:
            block_size = struct.unpack_from("!i", view, cursor)[0]
            # Skip the block size
            cursor += 4
            end = cursor + block_size
            # XXX snappy requires a bytes-like object but doesn't accept
            # a memoryview, so we must copy.
            out.append(snappy.decompress(view[cursor:end].tobytes()))
            cursor = end

        # See https://atleastfornow.net/blog/not-all-bytes/
        return b"".join(out)
    else:
        return snappy.decompress(payload)
