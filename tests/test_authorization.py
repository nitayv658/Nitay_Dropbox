"""
Folder Authorization Tests.

Verifies db.get_folder_permission() correctly resolves a caller's access level
to a folder: 'owner' via folders.owner_id, an explicit grant via folder_shares,
or None when the caller has neither. This helper is the foundation for closing
the IDOR gaps in metadata_service's REST endpoints.
"""

import pytest

import metadata_service.app.db as mdb
from tests.conftest import seed_folder, seed_user


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
