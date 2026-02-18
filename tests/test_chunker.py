"""
Unit tests for the File Chunking Utility (client_utils/chunker.py).

No database or network required — pure logic tests.
"""

import hashlib
import os
import tempfile

import pytest

from client_utils.chunker import CHUNK_SIZE, chunk_file


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _tmp(data: bytes) -> str:
    """Write bytes to a temporary file and return the path."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    f.write(data)
    f.close()
    return f.name


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_chunk_count_10mb():
    """10 MB file → exactly 3 chunks: [4MB, 4MB, 2MB]."""
    data = os.urandom(10 * 1024 * 1024)
    chunks = list(chunk_file(_tmp(data)))
    assert len(chunks) == 3


def test_chunk_sizes_10mb():
    """First two chunks are exactly CHUNK_SIZE; the last is the 2 MB remainder."""
    data = os.urandom(10 * 1024 * 1024)
    chunks = list(chunk_file(_tmp(data)))
    assert len(chunks[0][2]) == CHUNK_SIZE
    assert len(chunks[1][2]) == CHUNK_SIZE
    assert len(chunks[2][2]) == 2 * 1024 * 1024


def test_consistent_hashes_same_content():
    """Same file content always produces the same hash list (deterministic)."""
    data = os.urandom(8 * 1024 * 1024)
    path = _tmp(data)
    hashes_a = [h for _, h, _ in chunk_file(path)]
    hashes_b = [h for _, h, _ in chunk_file(path)]
    assert hashes_a == hashes_b


def test_single_byte_change_only_affects_first_chunk():
    """
    Flip one byte inside chunk 0. Only chunk 0's hash should change;
    chunks 1 and 2 must remain identical to the original.
    """
    data = bytearray(os.urandom(10 * 1024 * 1024))
    original = list(chunk_file(_tmp(bytes(data))))

    data[42] ^= 0xFF  # flip a byte well within the first 4 MB
    modified = list(chunk_file(_tmp(bytes(data))))

    assert original[0][1] != modified[0][1], "chunk 0 hash must change"
    assert original[1][1] == modified[1][1], "chunk 1 hash must be unchanged"
    assert original[2][1] == modified[2][1], "chunk 2 hash must be unchanged"


def test_single_byte_change_only_affects_last_chunk():
    """
    Flip one byte in the third chunk (offset > 8 MB).
    Only chunk 2's hash should change.
    """
    data = bytearray(os.urandom(10 * 1024 * 1024))
    original = list(chunk_file(_tmp(bytes(data))))

    data[9 * 1024 * 1024] ^= 0xFF  # inside the 2 MB tail chunk
    modified = list(chunk_file(_tmp(bytes(data))))

    assert original[0][1] == modified[0][1], "chunk 0 hash must be unchanged"
    assert original[1][1] == modified[1][1], "chunk 1 hash must be unchanged"
    assert original[2][1] != modified[2][1], "chunk 2 hash must change"


def test_hash_correctness():
    """Each chunk's reported hash must equal the real SHA-256 of that chunk's bytes."""
    data = os.urandom(6 * 1024 * 1024)  # two chunks
    for _, hash_hex, chunk_data in chunk_file(_tmp(data)):
        expected = hashlib.sha256(chunk_data).hexdigest()
        assert hash_hex == expected


def test_sequential_block_indices():
    """Block indices must start at 0 and be consecutive integers."""
    data = os.urandom(10 * 1024 * 1024)
    indices = [i for i, _, _ in chunk_file(_tmp(data))]
    assert indices == list(range(len(indices)))


def test_empty_file_yields_no_chunks():
    """An empty file produces an empty iterator."""
    chunks = list(chunk_file(_tmp(b"")))
    assert chunks == []


def test_sub_chunk_file_is_single_chunk():
    """A file smaller than CHUNK_SIZE is returned as exactly one chunk."""
    data = os.urandom(1024)  # 1 KB
    chunks = list(chunk_file(_tmp(data)))
    assert len(chunks) == 1
    assert chunks[0][0] == 0
    assert len(chunks[0][2]) == 1024


def test_exact_chunk_boundary():
    """A file exactly N * CHUNK_SIZE bytes produces exactly N chunks with no remainder."""
    n = 2
    data = os.urandom(n * CHUNK_SIZE)
    chunks = list(chunk_file(_tmp(data)))
    assert len(chunks) == n
    assert all(len(c[2]) == CHUNK_SIZE for c in chunks)
