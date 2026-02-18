"""
Interrupted Upload / Resumability Tests.

Verifies that the deduplication engine acts as a natural resumption mechanism:
when an upload is retried after a partial failure, already-uploaded blocks are
identified by hash and skipped (stored=False), so only the missing blocks are
actually transferred and written.
"""

import hashlib
import os
import tempfile

import pytest

from client_utils.chunker import CHUNK_SIZE, chunk_file


async def _upload_block(client, data: bytes) -> dict:
    h = hashlib.sha256(data).hexdigest()
    resp = await client.post(
        "/blocks/upload",
        params={"hash": h, "size": len(data)},
        files={"file": ("block", data, "application/octet-stream")},
    )
    resp.raise_for_status()
    return resp.json()


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_resume_skips_already_uploaded_blocks(block_client, in_memory_storage):
    """
    Scenario — interrupted upload:
      Phase 1: upload chunks 0 and 1 (network 'fails' after chunk 1)
      Phase 2: retry the full upload from chunk 0

    Expected behaviour:
      - Chunks 0-1 in phase 2 → stored=False (deduplication hit, skipped)
      - Chunks 2-4 in phase 2 → stored=True  (new blocks written to storage)
      - Physical storage contains exactly 5 unique blocks total
    """
    # Build a 5-chunk file (5 × CHUNK_SIZE = 20 MB)
    data = os.urandom(5 * CHUNK_SIZE)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(data)
        tmp_path = f.name

    chunks = list(chunk_file(tmp_path))
    assert len(chunks) == 5

    # Phase 1: upload first two chunks only (simulated network failure)
    phase1 = [await _upload_block(block_client, c[2]) for c in chunks[:2]]
    assert all(r["stored"] is True for r in phase1), "First two blocks must be stored"
    assert len(in_memory_storage) == 2

    # Phase 2: retry the full upload from the beginning
    phase2 = [await _upload_block(block_client, c[2]) for c in chunks]

    assert phase2[0]["stored"] is False, "Chunk 0 already uploaded — must be skipped"
    assert phase2[1]["stored"] is False, "Chunk 1 already uploaded — must be skipped"
    assert phase2[2]["stored"] is True,  "Chunk 2 is new — must be stored"
    assert phase2[3]["stored"] is True,  "Chunk 3 is new — must be stored"
    assert phase2[4]["stored"] is True,  "Chunk 4 is new — must be stored"

    assert len(in_memory_storage) == 5, "Exactly 5 unique blocks in storage"


async def test_full_retry_is_idempotent(block_client, in_memory_storage):
    """
    Uploading the same set of blocks a second time is a no-op for storage:
      - All blocks on the second run → stored=False
      - Storage size unchanged after the second run
    """
    blocks = [os.urandom(256 * 1024) for _ in range(3)]

    first_run = [await _upload_block(block_client, d) for d in blocks]
    assert all(r["stored"] is True for r in first_run)

    second_run = [await _upload_block(block_client, d) for d in blocks]
    assert all(r["stored"] is False for r in second_run), (
        "Retrying the same blocks must never write to storage again"
    )
    assert len(in_memory_storage) == 3


async def test_partial_overlap_upload(block_client, in_memory_storage):
    """
    A resumed upload where only some chunks overlap with the previous attempt:
      - 3 blocks total; first attempt uploads blocks [0, 1]
      - Second attempt uploads blocks [1, 2] (block 1 overlaps)

    Expected: block 0 missing from final run but still in storage;
              block 1 → stored=False; block 2 → stored=True.
    """
    blocks = [os.urandom(256 * 1024) for _ in range(3)]

    # First attempt: blocks 0 and 1
    await _upload_block(block_client, blocks[0])
    await _upload_block(block_client, blocks[1])
    assert len(in_memory_storage) == 2

    # Second attempt: blocks 1 and 2
    r1 = await _upload_block(block_client, blocks[1])
    r2 = await _upload_block(block_client, blocks[2])

    assert r1["stored"] is False, "Block 1 already exists — deduped"
    assert r2["stored"] is True,  "Block 2 is new — stored"
    assert len(in_memory_storage) == 3


async def test_has_block_can_be_used_to_skip_upload(block_client, in_memory_storage):
    """
    A smart client can call GET /blocks/has/{hash} before uploading to skip
    blocks that already exist without sending the data at all.
    """
    data = os.urandom(256 * 1024)
    h = hashlib.sha256(data).hexdigest()

    # Block not yet uploaded
    before = await block_client.get(f"/blocks/has/{h}")
    assert before.json()["exists"] is False

    # Upload it
    await _upload_block(block_client, data)

    # Now the smart client would skip this block on retry
    after = await block_client.get(f"/blocks/has/{h}")
    assert after.json()["exists"] is True
    assert after.json()["s3_key"] != ""
