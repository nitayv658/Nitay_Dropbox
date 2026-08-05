"""
Synchronization Service Stress Tests.

Exercises the Sync Engine under concurrent load:
  - 5+ devices calling get_missing_blocks simultaneously
  - Device cursors advancing correctly after sync
  - 10 concurrent version commits with unique version numbers
"""

import asyncio
import hashlib
import os

import pytest

from tests.conftest import seed_folder, seed_user


async def _upload_block(block_client, size_kb: int = 256) -> str:
    """Upload a fresh random block and return its hash."""
    data = os.urandom(size_kb * 1024)
    h = hashlib.sha256(data).hexdigest()
    resp = await block_client.post(
        "/blocks/upload",
        params={"hash": h, "size": len(data)},
        files={"file": ("block", data, "application/octet-stream")},
    )
    assert resp.status_code == 200
    return h


async def _seed_device(pool, user_id: str, name: str) -> str:
    return await pool.fetchval(
        "INSERT INTO devices (user_id, name) VALUES ($1::uuid, $2) RETURNING id::text",
        user_id,
        name,
    )


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_five_devices_get_correct_missing_blocks(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    5 devices (all on version 0 / no cursor) query for missing blocks concurrently.
    Each must receive the complete set of block hashes for the current file version.
    """
    import metadata_service.app.db as mdb

    mdb.pool = db_pool  # ensure the db module uses the test pool

    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    # Upload 3 blocks and commit a version
    hashes = [await _upload_block(block_client) for _ in range(3)]

    override_user(user)
    file_id = (
        await meta_client.post(
            "/files", json={"folder_id": folder, "name": "shared.bin"}
        )
    ).json()["file_id"]

    await meta_client.post(
        f"/files/{file_id}/versions",
        json={"user_id": user, "block_hashes": hashes},
    )

    # Create 5 devices
    device_ids = [await _seed_device(db_pool, user, f"device-{i}") for i in range(5)]

    # All 5 query missing blocks concurrently (no prior cursor → need everything)
    results = await asyncio.gather(
        *[mdb.get_missing_blocks(did, file_id, None) for did in device_ids]
    )

    expected = set(hashes)
    for i, missing in enumerate(results):
        assert set(missing) == expected, (
            f"Device {i} received wrong missing blocks.\n"
            f"Expected: {expected}\nGot: {set(missing)}"
        )


async def test_device_cursor_advances_eliminates_missing_blocks(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    After a device syncs (cursor advanced to current version),
    a subsequent call to get_missing_blocks returns an empty list.
    """
    import metadata_service.app.db as mdb

    mdb.pool = db_pool

    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    h = await _upload_block(block_client)
    override_user(user)
    file_id = (
        await meta_client.post(
            "/files", json={"folder_id": folder, "name": "doc.txt"}
        )
    ).json()["file_id"]

    ver_resp = await meta_client.post(
        f"/files/{file_id}/versions",
        json={"user_id": user, "block_hashes": [h]},
    )
    version_id = ver_resp.json()["version_id"]

    device_id = await _seed_device(db_pool, user, "laptop")

    # Before cursor: device is missing the block
    missing_before = await mdb.get_missing_blocks(device_id, file_id, None)
    assert h in missing_before

    # Advance cursor to current version (device has downloaded all blocks)
    await mdb.upsert_device_cursor(device_id, file_id, version_id)

    # After cursor: device needs nothing
    missing_after = await mdb.get_missing_blocks(device_id, file_id, version_id)
    assert missing_after == [], f"Expected no missing blocks, got: {missing_after}"


async def test_new_version_appears_as_missing_after_cursor_set(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    Device is up to date on version 1. A new version 2 is committed.
    get_missing_blocks must now return only the blocks that are new in version 2.
    """
    import metadata_service.app.db as mdb

    mdb.pool = db_pool

    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)
    device_id = await _seed_device(db_pool, user, "phone")

    # Version 1: one shared block
    h_shared = await _upload_block(block_client)
    override_user(user)
    file_id = (
        await meta_client.post(
            "/files", json={"folder_id": folder, "name": "evolving.txt"}
        )
    ).json()["file_id"]

    v1 = (
        await meta_client.post(
            f"/files/{file_id}/versions",
            json={"user_id": user, "block_hashes": [h_shared]},
        )
    ).json()["version_id"]

    # Device syncs to version 1
    await mdb.upsert_device_cursor(device_id, file_id, v1)
    assert await mdb.get_missing_blocks(device_id, file_id, v1) == []

    # Version 2: shared block + one new block
    h_new = await _upload_block(block_client)
    await meta_client.post(
        f"/files/{file_id}/versions",
        json={"user_id": user, "block_hashes": [h_shared, h_new]},
    )

    # Device still on version 1 cursor — must see only the new block
    missing = await mdb.get_missing_blocks(device_id, file_id, v1)
    assert missing == [h_new], (
        f"Only the new block should be missing, got: {missing}"
    )


async def test_ten_concurrent_commits_all_unique_versions(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    10 simultaneous version commits to the same file.
    All must succeed and produce 10 unique version numbers (stress test of
    the FOR UPDATE serialization lock).
    """
    N = 10
    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    hashes = [await _upload_block(block_client) for _ in range(N)]

    override_user(user)
    file_id = (
        await meta_client.post(
            "/files",
            json={"folder_id": folder, "name": "contested.bin"},
        )
    ).json()["file_id"]

    responses = await asyncio.gather(
        *[
            meta_client.post(
                f"/files/{file_id}/versions",
                json={"user_id": user, "block_hashes": [h]},
            )
            for h in hashes
        ]
    )

    failed = [r for r in responses if r.status_code != 201]
    assert not failed, f"{len(failed)} commit(s) failed: {[r.text for r in failed]}"

    history = (
        await meta_client.get(f"/files/{file_id}/history", params={"limit": N + 1})
    ).json()["versions"]

    assert len(history) == N, f"Expected {N} versions, got {len(history)}"

    nums = [v["version_number"] for v in history]
    assert len(set(nums)) == N, (
        f"Duplicate version numbers found: {sorted(nums)} — "
        "concurrent lock is not working correctly"
    )
