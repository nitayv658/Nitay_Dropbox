"""
Sync Conflict & Race Condition Tests.

Simulates two clients updating the same file concurrently and verifies:
  1. Both writes succeed (no data loss).
  2. Both versions appear in history with unique version numbers.

The `SELECT ... FOR UPDATE` lock added to commit_file_version ensures unique
version numbers even under concurrent load. The third test is the regression
test that would fail without that fix.
"""

import asyncio
import hashlib
import os

import pytest

from tests.conftest import seed_folder, seed_user


async def _upload_block(client, size_kb: int = 256) -> tuple[str, bytes]:
    """Upload a fresh random block, return (hash_hex, data)."""
    data = os.urandom(size_kb * 1024)
    h = hashlib.sha256(data).hexdigest()
    resp = await client.post(
        "/blocks/upload",
        params={"hash": h, "size": len(data)},
        files={"file": ("block", data, "application/octet-stream")},
    )
    assert resp.status_code == 200
    return h, data


async def _commit(meta_client, file_id: str, user_id: str, hash_hex: str):
    return await meta_client.post(
        f"/files/{file_id}/versions",
        json={"user_id": user_id, "block_hashes": [hash_hex]},
    )


# ─── Tests ────────────────────────────────────────────────────────────────────


async def test_concurrent_writes_both_succeed(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    Two devices commit different versions of the same file at the same time.
    Both HTTP responses must be 201 Created — no write is silently dropped.
    """
    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    h_a, _ = await _upload_block(block_client)
    h_b, _ = await _upload_block(block_client)

    override_user(user)
    file_id = (
        await meta_client.post(
            "/files",
            json={"folder_id": folder, "name": "config.json"},
        )
    ).json()["file_id"]

    resp_a, resp_b = await asyncio.gather(
        _commit(meta_client, file_id, user, h_a),
        _commit(meta_client, file_id, user, h_b),
    )

    assert resp_a.status_code == 201, f"Device A commit failed: {resp_a.text}"
    assert resp_b.status_code == 201, f"Device B commit failed: {resp_b.text}"


async def test_no_data_loss_sequential_writes(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    Sequential writes (simulating 'last writer wins') preserve both versions
    in the file history — neither write is lost.
    """
    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    h_a, _ = await _upload_block(block_client)
    h_b, _ = await _upload_block(block_client)

    override_user(user)
    file_id = (
        await meta_client.post(
            "/files",
            json={"folder_id": folder, "name": "config.json"},
        )
    ).json()["file_id"]

    await _commit(meta_client, file_id, user, h_a)
    await _commit(meta_client, file_id, user, h_b)

    resp = await meta_client.get(f"/files/{file_id}/history")
    versions = resp.json()["versions"]

    assert len(versions) == 2, "Both writes must be preserved in history"
    nums = sorted(v["version_number"] for v in versions)
    assert nums == [1, 2], f"Unexpected version numbers: {nums}"


async def test_concurrent_version_numbers_unique(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """
    Regression test for the FOR UPDATE race condition fix.

    5 concurrent commits to the same file must produce 5 unique version numbers.
    Without the row-level lock, two transactions could both read max_version=0
    and both insert version_number=1, causing silent data corruption.
    """
    N = 5
    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    hashes = [(await _upload_block(block_client))[0] for _ in range(N)]

    override_user(user)
    file_id = (
        await meta_client.post(
            "/files",
            json={"folder_id": folder, "name": "shared.txt"},
        )
    ).json()["file_id"]

    responses = await asyncio.gather(
        *[_commit(meta_client, file_id, user, h) for h in hashes]
    )

    assert all(
        r.status_code == 201 for r in responses
    ), "All concurrent commits must succeed"

    history = (
        await meta_client.get(f"/files/{file_id}/history", params={"limit": N + 1})
    ).json()["versions"]

    assert len(history) == N, f"Expected {N} versions, found {len(history)}"

    nums = [v["version_number"] for v in history]
    assert len(set(nums)) == N, (
        f"Duplicate version numbers detected: {sorted(nums)} — "
        "FOR UPDATE lock may not be working"
    )


async def test_history_is_ordered_newest_first(
    block_client, meta_client, override_user, db_pool, in_memory_storage
):
    """File history endpoint returns versions in descending version_number order."""
    user = await seed_user(db_pool)
    folder = await seed_folder(db_pool, user)

    override_user(user)
    file_id = (
        await meta_client.post(
            "/files",
            json={"folder_id": folder, "name": "ordered.txt"},
        )
    ).json()["file_id"]

    for _ in range(3):
        h, _ = await _upload_block(block_client)
        await _commit(meta_client, file_id, user, h)

    versions = (
        await meta_client.get(f"/files/{file_id}/history")
    ).json()["versions"]

    nums = [v["version_number"] for v in versions]
    assert nums == sorted(nums, reverse=True), "History must be newest-first"
