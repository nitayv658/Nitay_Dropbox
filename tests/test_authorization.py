"""
Folder Authorization Tests.

Verifies db.get_folder_permission() correctly resolves a caller's access level
to a folder: 'owner' via folders.owner_id, an explicit grant via folder_shares,
or None when the caller has neither. This helper is the foundation for closing
the IDOR gaps in metadata_service's REST endpoints.
"""

import hashlib
import os

import pytest

import metadata_service.app.db as mdb
from tests.conftest import seed_folder, seed_user


async def _upload_block(block_client, size_kb: int = 64) -> str:
    data = os.urandom(size_kb * 1024)
    h = hashlib.sha256(data).hexdigest()
    resp = await block_client.post(
        "/blocks/upload",
        params={"hash": h, "size": len(data)},
        files={"file": ("block", data, "application/octet-stream")},
    )
    assert resp.status_code == 200
    return h


@pytest.fixture(autouse=True)
def _wire_pool(db_pool):
    mdb.pool = db_pool


async def test_owner_has_owner_permission(db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)

    permission = await mdb.get_folder_permission(owner, folder)

    assert permission == "owner"


async def test_grantee_has_shared_permission(db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)

    await mdb.grant_folder_access(folder, grantee, "write", owner)

    permission = await mdb.get_folder_permission(grantee, folder)

    assert permission == "write"


async def test_unrelated_user_has_no_permission(db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)

    permission = await mdb.get_folder_permission(stranger, folder)

    assert permission is None


async def test_nonexistent_folder_has_no_permission(db_pool):
    user = await seed_user(db_pool)
    fake_folder_id = "00000000-0000-0000-0000-000000000099"

    permission = await mdb.get_folder_permission(user, fake_folder_id)

    assert permission is None


# ─── POST /directories ─────────────────────────────────────────────────────────


async def test_create_directory_owner_is_authenticated_user(
    meta_client, override_user, db_pool
):
    """The folder's owner must be whoever the JWT identifies, not a client-supplied value."""
    alice = await seed_user(db_pool, "alice@example.com")
    override_user(alice)

    resp = await meta_client.post("/directories", json={"name": "root"})

    assert resp.status_code == 201
    folder_id = resp.json()["folder_id"]
    owner = await db_pool.fetchval(
        "SELECT owner_id::text FROM folders WHERE id = $1::uuid", folder_id
    )
    assert owner == alice


async def test_create_directory_ignores_spoofed_user_id_in_body(
    meta_client, override_user, db_pool
):
    """Regression test for the IDOR: a spoofed user_id in the body must be ignored."""
    alice = await seed_user(db_pool, "alice@example.com")
    bob = await seed_user(db_pool, "bob@example.com")
    override_user(alice)

    resp = await meta_client.post(
        "/directories", json={"name": "root", "user_id": bob}
    )

    assert resp.status_code == 201
    folder_id = resp.json()["folder_id"]
    owner = await db_pool.fetchval(
        "SELECT owner_id::text FROM folders WHERE id = $1::uuid", folder_id
    )
    assert owner == alice, "owner must be the authenticated caller, not the spoofed body field"


async def test_create_subdirectory_denied_without_parent_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    parent = await seed_folder(db_pool, owner)
    override_user(stranger)

    resp = await meta_client.post(
        "/directories", json={"name": "sub", "parent_folder_id": parent}
    )

    assert resp.status_code == 403


async def test_create_subdirectory_allowed_for_parent_owner(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    parent = await seed_folder(db_pool, owner)
    override_user(owner)

    resp = await meta_client.post(
        "/directories", json={"name": "sub", "parent_folder_id": parent}
    )

    assert resp.status_code == 201


async def test_create_subdirectory_allowed_with_shared_write_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    parent = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(parent, grantee, "write", owner)
    override_user(grantee)

    resp = await meta_client.post(
        "/directories", json={"name": "sub", "parent_folder_id": parent}
    )

    assert resp.status_code == 201


async def test_create_subdirectory_denied_with_shared_read_only_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    parent = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(parent, grantee, "read", owner)
    override_user(grantee)

    resp = await meta_client.post(
        "/directories", json={"name": "sub", "parent_folder_id": parent}
    )

    assert resp.status_code == 403


async def test_create_root_directory_without_parent_requires_no_permission_check(
    meta_client, override_user, db_pool
):
    """Creating a top-level (no parent) folder is always allowed for any authenticated user."""
    user = await seed_user(db_pool, "anyone@example.com")
    override_user(user)

    resp = await meta_client.post("/directories", json={"name": "root"})

    assert resp.status_code == 201


# ─── POST /files ────────────────────────────────────────────────────────────


async def test_create_file_denied_without_folder_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(stranger)

    resp = await meta_client.post(
        "/files", json={"folder_id": folder, "name": "secret.txt"}
    )

    assert resp.status_code == 403


async def test_create_file_allowed_for_folder_owner(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)

    resp = await meta_client.post(
        "/files", json={"folder_id": folder, "name": "notes.txt"}
    )

    assert resp.status_code == 201


async def test_create_file_allowed_with_shared_write_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "write", owner)
    override_user(grantee)

    resp = await meta_client.post(
        "/files", json={"folder_id": folder, "name": "shared.txt"}
    )

    assert resp.status_code == 201


async def test_create_file_denied_with_shared_read_only_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "read", owner)
    override_user(grantee)

    resp = await meta_client.post(
        "/files", json={"folder_id": folder, "name": "readonly.txt"}
    )

    assert resp.status_code == 403


# ─── POST /files/{file_id}/versions ────────────────────────────────────────────


async def test_commit_version_denied_without_folder_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    override_user(stranger)
    resp = await meta_client.post(
        f"/files/{file_id}/versions",
        json={"block_hashes": ["0" * 64]},
    )

    assert resp.status_code == 403


async def test_commit_version_allowed_for_folder_owner(
    meta_client, block_client, override_user, in_memory_storage, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]
    h = await _upload_block(block_client)

    resp = await meta_client.post(
        f"/files/{file_id}/versions", json={"block_hashes": [h]}
    )

    assert resp.status_code == 201


async def test_commit_version_allowed_with_shared_write_access(
    meta_client, block_client, override_user, in_memory_storage, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "write", owner)

    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]
    h = await _upload_block(block_client)

    override_user(grantee)
    resp = await meta_client.post(
        f"/files/{file_id}/versions", json={"block_hashes": [h]}
    )

    assert resp.status_code == 201


async def test_commit_version_denied_with_shared_read_only_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "read", owner)

    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    override_user(grantee)
    resp = await meta_client.post(
        f"/files/{file_id}/versions",
        json={"block_hashes": ["0" * 64]},
    )

    assert resp.status_code == 403


async def test_commit_version_ignores_spoofed_user_id_in_body(
    meta_client, block_client, override_user, in_memory_storage, db_pool
):
    """Regression test for the IDOR: created_by_user must come from the token."""
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]
    h = await _upload_block(block_client)

    resp = await meta_client.post(
        f"/files/{file_id}/versions",
        json={"user_id": stranger, "block_hashes": [h]},
    )

    assert resp.status_code == 201
    version_id = resp.json()["version_id"]
    created_by = await db_pool.fetchval(
        "SELECT created_by_user::text FROM file_versions WHERE id = $1::uuid",
        version_id,
    )
    assert created_by == owner, "created_by_user must be the token identity, not the spoofed body field"


# ─── DELETE /files/{file_id} ───────────────────────────────────────────────────


async def test_delete_file_denied_for_non_owner(meta_client, override_user, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    override_user(stranger)
    resp = await meta_client.delete(f"/files/{file_id}")

    assert resp.status_code == 404


async def test_delete_file_ignores_spoofed_user_id_query_param(
    meta_client, override_user, db_pool
):
    """Regression test for the IDOR: a spoofed user_id query param must not authorize deletion."""
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    override_user(stranger)
    resp = await meta_client.delete(f"/files/{file_id}", params={"user_id": owner})

    assert resp.status_code == 404
    still_active = await db_pool.fetchval(
        "SELECT NOT deleted FROM files WHERE id = $1::uuid", file_id
    )
    assert still_active is True, "file must not be deleted by a spoofed query param"


async def test_delete_file_allowed_for_owner(meta_client, override_user, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    resp = await meta_client.delete(f"/files/{file_id}")

    assert resp.status_code == 200


# ─── GET /files/{file_id}/history ──────────────────────────────────────────────


async def test_file_history_denied_without_folder_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    override_user(stranger)
    resp = await meta_client.get(f"/files/{file_id}/history")

    assert resp.status_code == 403


async def test_file_history_allowed_for_owner(meta_client, override_user, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    resp = await meta_client.get(f"/files/{file_id}/history")

    assert resp.status_code == 200


async def test_file_history_allowed_with_shared_read_access(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "read", owner)
    override_user(owner)
    file_id = (
        await meta_client.post("/files", json={"folder_id": folder, "name": "doc.txt"})
    ).json()["file_id"]

    override_user(grantee)
    resp = await meta_client.get(f"/files/{file_id}/history")

    assert resp.status_code == 200


async def test_file_history_denied_for_nonexistent_file(meta_client, override_user, db_pool):
    user = await seed_user(db_pool)
    override_user(user)

    resp = await meta_client.get("/files/00000000-0000-0000-0000-000000000099/history")

    assert resp.status_code == 404


# ─── POST /folders/{folder_id}/shares ──────────────────────────────────────────


async def test_grant_folder_access_denied_for_non_owner(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    someone_else = await seed_user(db_pool, "someone_else@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(stranger)

    resp = await meta_client.post(
        f"/folders/{folder}/shares",
        json={"grantee_user_id": someone_else, "permission": "read"},
    )

    assert resp.status_code == 403


async def test_grant_folder_access_denied_for_write_grantee(
    meta_client, override_user, db_pool
):
    """Having write access to a folder doesn't grant the right to re-share it."""
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    someone_else = await seed_user(db_pool, "someone_else@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "write", owner)
    override_user(grantee)

    resp = await meta_client.post(
        f"/folders/{folder}/shares",
        json={"grantee_user_id": someone_else, "permission": "read"},
    )

    assert resp.status_code == 403


async def test_grant_folder_access_allowed_for_owner(
    meta_client, override_user, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)

    resp = await meta_client.post(
        f"/folders/{folder}/shares",
        json={"grantee_user_id": grantee, "permission": "read"},
    )

    assert resp.status_code == 201
    created_by = await db_pool.fetchval(
        "SELECT created_by::text FROM folder_shares WHERE folder_id = $1::uuid AND grantee_user_id = $2::uuid",
        folder,
        grantee,
    )
    assert created_by == owner


async def test_grant_folder_access_ignores_spoofed_granted_by(
    meta_client, override_user, db_pool
):
    """Regression test for the IDOR: a spoofed granted_by in the body must be ignored."""
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    someone_else = await seed_user(db_pool, "someone_else@example.com")
    folder = await seed_folder(db_pool, owner)
    override_user(owner)

    resp = await meta_client.post(
        f"/folders/{folder}/shares",
        json={
            "grantee_user_id": grantee,
            "permission": "read",
            "granted_by": someone_else,
        },
    )

    assert resp.status_code == 201
    created_by = await db_pool.fetchval(
        "SELECT created_by::text FROM folder_shares WHERE folder_id = $1::uuid AND grantee_user_id = $2::uuid",
        folder,
        grantee,
    )
    assert created_by == owner, "created_by must be the token identity, not the spoofed body field"
