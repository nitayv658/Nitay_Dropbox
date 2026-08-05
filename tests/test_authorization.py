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
