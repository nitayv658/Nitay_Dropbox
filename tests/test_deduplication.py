"""
Deduplication Integration Tests.

Verifies that identical blocks are stored only once regardless of how many
users upload them, and that the Metadata DB correctly tracks separate file
entries with a shared ref-counted block.
"""

import hashlib
import os

import pytest

from tests.conftest import seed_folder, seed_user


def _block(size_kb: int = 512) -> tuple[bytes, str]:
    """Return (data, sha256_hex) for a random block of the given size."""
    data = os.urandom(size_kb * 1024)
    return data, hashlib.sha256(data).hexdigest()


async def _upload(client, data: bytes, hash_hex: str):
    return await client.post(
        "/blocks/upload",
        params={"hash": hash_hex, "size": len(data)},
        files={"file": ("block", data, "application/octet-stream")},
    )


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_identical_block_stored_once(block_client, in_memory_storage):
    """
    Uploading the same block twice:
      - First call  → stored=True  (written to storage)
      - Second call → stored=False (deduplication hit)
      - Exactly 1 entry in physical storage
    """
    data, h = _block()

    r1 = await _upload(block_client, data, h)
    assert r1.status_code == 200
    assert r1.json()["stored"] is True

    r2 = await _upload(block_client, data, h)
    assert r2.status_code == 200
    assert r2.json()["stored"] is False

    assert len(in_memory_storage) == 1


async def test_ref_count_increments_on_dedup(block_client, db_pool):
    """ref_count in the blocks table must reach 2 after two uploads of the same block."""
    data, h = _block()

    await _upload(block_client, data, h)
    await _upload(block_client, data, h)

    ref_count = await db_pool.fetchval(
        "SELECT ref_count FROM blocks WHERE hash = $1", h
    )
    assert ref_count == 2


async def test_two_users_same_file_one_storage_two_metadata(
    block_client, meta_client, db_pool, in_memory_storage
):
    """
    Full deduplication scenario:
      1. User A uploads image.png
      2. User B uploads an identical copy
      3. Assert: only 1 block in physical storage
      4. Assert: 2 distinct file rows in the Metadata DB
      5. Assert: 2 file_version rows (one per file)
      6. Assert: blocks.ref_count == 2
    """
    user_a = await seed_user(db_pool, "alice@example.com")
    user_b = await seed_user(db_pool, "bob@example.com")
    folder_a = await seed_folder(db_pool, user_a, "Alice Root")
    folder_b = await seed_folder(db_pool, user_b, "Bob Root")

    data, h = _block()

    # First upload (stored)
    r1 = await _upload(block_client, data, h)
    assert r1.json()["stored"] is True

    # Second upload (deduped)
    r2 = await _upload(block_client, data, h)
    assert r2.json()["stored"] is False

    # Create file + version for User A
    fa = (
        await meta_client.post(
            "/files", json={"user_id": user_a, "folder_id": folder_a, "name": "image.png"}
        )
    ).json()["file_id"]
    await meta_client.post(
        f"/files/{fa}/versions",
        json={"user_id": user_a, "block_hashes": [h]},
    )

    # Create file + version for User B
    fb = (
        await meta_client.post(
            "/files", json={"user_id": user_b, "folder_id": folder_b, "name": "image.png"}
        )
    ).json()["file_id"]
    await meta_client.post(
        f"/files/{fb}/versions",
        json={"user_id": user_b, "block_hashes": [h]},
    )

    # 1 physical block
    assert len(in_memory_storage) == 1, "Block data must be stored exactly once"

    # 2 file rows
    file_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM files WHERE deleted = FALSE"
    )
    assert file_count == 2

    # 2 version rows
    version_count = await db_pool.fetchval("SELECT COUNT(*) FROM file_versions")
    assert version_count == 2

    # ref_count == 4:
    #   +1 from first upload (insert_block sets ref_count=1)
    #   +1 from second upload (dedup path calls increment_refcount)
    #   +1 from commit_file_version for User A
    #   +1 from commit_file_version for User B
    ref_count = await db_pool.fetchval(
        "SELECT ref_count FROM blocks WHERE hash = $1", h
    )
    assert ref_count == 4


async def test_distinct_blocks_stored_separately(block_client, in_memory_storage):
    """Two blocks with different content are always stored as separate entries."""
    data_a, h_a = _block()
    data_b, h_b = _block()
    assert h_a != h_b  # sanity: two random 512 KB blocks must differ

    r_a = await _upload(block_client, data_a, h_a)
    r_b = await _upload(block_client, data_b, h_b)

    assert r_a.json()["stored"] is True
    assert r_b.json()["stored"] is True
    assert len(in_memory_storage) == 2


async def test_has_block_reflects_dedup_state(block_client, in_memory_storage):
    """GET /blocks/has/{hash} returns exists=True only after the block is uploaded."""
    data, h = _block()

    before = await block_client.get(f"/blocks/has/{h}")
    assert before.json()["exists"] is False

    await _upload(block_client, data, h)

    after = await block_client.get(f"/blocks/has/{h}")
    assert after.json()["exists"] is True
