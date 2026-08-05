"""
gRPC Authentication Tests.

Verifies JWTAuthInterceptor rejects calls without a valid Bearer JWT before
they reach any servicer method, and passes authenticated calls through.
Uses a real in-process grpc.aio server + channel (grpc_server_address fixture
in conftest.py) — no mocking of the gRPC transport itself.
"""

import hashlib

import grpc
import pytest

import metadata_service.app.db as mdb
from metadata_service.app import internal_pb2, internal_pb2_grpc
from metadata_service.app.auth import create_access_token
from block_server.app.config import settings as block_settings
from proto import internal_pb2 as block_pb2
from proto import internal_pb2_grpc as block_pb2_grpc
from tests.conftest import seed_folder, seed_user


def _bearer_metadata(token: str):
    return (("authorization", f"Bearer {token}"),)


async def _seed_device(pool, user_id: str, name: str = "device") -> str:
    return await pool.fetchval(
        "INSERT INTO devices (user_id, name) VALUES ($1::uuid, $2) RETURNING id::text",
        user_id,
        name,
    )


async def test_call_without_token_is_rejected_as_unauthenticated(
    grpc_server_address, db_pool
):
    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CreateDirectory(
                internal_pb2.CreateDirectoryRequest(user_id="irrelevant", name="root")
            )

    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_call_with_garbage_token_is_rejected_as_unauthenticated(
    grpc_server_address, db_pool
):
    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CreateDirectory(
                internal_pb2.CreateDirectoryRequest(user_id="irrelevant", name="root"),
                metadata=_bearer_metadata("not-a-real-jwt"),
            )

    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_call_with_valid_token_succeeds(grpc_server_address, db_pool):
    user = await seed_user(db_pool, "grpc-user@example.com")
    token = create_access_token(user, "grpc-user@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        resp = await stub.CreateDirectory(
            internal_pb2.CreateDirectoryRequest(user_id=user, name="root"),
            metadata=_bearer_metadata(token),
        )

    assert resp.folder_id


# ─── MetadataService.CreateDirectory ───────────────────────────────────────────


async def test_grpc_create_directory_owner_is_authenticated_user(
    grpc_server_address, db_pool
):
    user = await seed_user(db_pool, "grpc-owner@example.com")
    token = create_access_token(user, "grpc-owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        resp = await stub.CreateDirectory(
            internal_pb2.CreateDirectoryRequest(name="root"),
            metadata=_bearer_metadata(token),
        )

    owner = await db_pool.fetchval(
        "SELECT owner_id::text FROM folders WHERE id = $1::uuid", resp.folder_id
    )
    assert owner == user


async def test_grpc_create_directory_ignores_spoofed_user_id(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        resp = await stub.CreateDirectory(
            internal_pb2.CreateDirectoryRequest(user_id=stranger, name="root"),
            metadata=_bearer_metadata(token),
        )

    folder_owner = await db_pool.fetchval(
        "SELECT owner_id::text FROM folders WHERE id = $1::uuid", resp.folder_id
    )
    assert folder_owner == owner, "owner must be the token identity, not the spoofed request field"


async def test_grpc_create_subdirectory_denied_without_parent_access(
    grpc_server_address, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    parent = await seed_folder(db_pool, owner)
    token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CreateDirectory(
                internal_pb2.CreateDirectoryRequest(parent_folder_id=parent, name="sub"),
                metadata=_bearer_metadata(token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_create_subdirectory_allowed_for_parent_owner(
    grpc_server_address, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    parent = await seed_folder(db_pool, owner)
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        resp = await stub.CreateDirectory(
            internal_pb2.CreateDirectoryRequest(parent_folder_id=parent, name="sub"),
            metadata=_bearer_metadata(token),
        )

    assert resp.folder_id


# ─── MetadataService.DeleteFile ────────────────────────────────────────────────


async def test_grpc_delete_file_denied_for_non_owner(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    stranger_token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.DeleteFile(
                internal_pb2.DeleteFileRequest(file_id=file_id),
                metadata=_bearer_metadata(stranger_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


async def test_grpc_delete_file_ignores_spoofed_user_id(grpc_server_address, db_pool):
    """Regression test for the IDOR: a spoofed user_id in the request must not authorize deletion."""
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    stranger_token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.DeleteFile(
                internal_pb2.DeleteFileRequest(user_id=owner, file_id=file_id),
                metadata=_bearer_metadata(stranger_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND
    still_active = await db_pool.fetchval(
        "SELECT NOT deleted FROM files WHERE id = $1::uuid", file_id
    )
    assert still_active is True, "file must not be deleted by a spoofed user_id field"


async def test_grpc_delete_file_allowed_for_owner(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        resp = await stub.DeleteFile(
            internal_pb2.DeleteFileRequest(file_id=file_id),
            metadata=_bearer_metadata(token),
        )

    assert resp.ok is True


# ─── MetadataService.GetFileHistory ────────────────────────────────────────────


async def test_grpc_file_history_denied_without_folder_access(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    stranger_token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetFileHistory(
                internal_pb2.FileHistoryRequest(file_id=file_id),
                metadata=_bearer_metadata(stranger_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_file_history_allowed_for_owner(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        resp = await stub.GetFileHistory(
            internal_pb2.FileHistoryRequest(file_id=file_id),
            metadata=_bearer_metadata(token),
        )

    assert list(resp.versions) == []


async def test_grpc_file_history_denied_for_nonexistent_file(grpc_server_address, db_pool):
    user = await seed_user(db_pool)
    token = create_access_token(user, "x@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.MetadataServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetFileHistory(
                internal_pb2.FileHistoryRequest(
                    file_id="00000000-0000-0000-0000-000000000099"
                ),
                metadata=_bearer_metadata(token),
            )

    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


# ─── SyncService.GetMissingBlocks ──────────────────────────────────────────────


async def test_grpc_get_missing_blocks_denied_for_device_not_owned_by_caller(
    grpc_server_address, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    device_id = await _seed_device(db_pool, owner)
    stranger_token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SyncServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetMissingBlocks(
                internal_pb2.MissingBlocksRequest(device_id=device_id, file_id=file_id),
                metadata=_bearer_metadata(stranger_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_get_missing_blocks_denied_without_folder_access(
    grpc_server_address, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    # stranger's own device, but stranger has no access to owner's folder/file
    device_id = await _seed_device(db_pool, stranger)
    stranger_token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SyncServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetMissingBlocks(
                internal_pb2.MissingBlocksRequest(device_id=device_id, file_id=file_id),
                metadata=_bearer_metadata(stranger_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_get_missing_blocks_allowed_for_owner_with_own_device(
    grpc_server_address, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    device_id = await _seed_device(db_pool, owner)
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SyncServiceStub(channel)
        resp = await stub.GetMissingBlocks(
            internal_pb2.MissingBlocksRequest(device_id=device_id, file_id=file_id),
            metadata=_bearer_metadata(token),
        )

    assert list(resp.missing_block_hashes) == []


async def test_grpc_get_missing_blocks_denied_for_nonexistent_device(
    grpc_server_address, db_pool
):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    file_id = await mdb.create_file(folder, "doc.txt")
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SyncServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetMissingBlocks(
                internal_pb2.MissingBlocksRequest(
                    device_id="00000000-0000-0000-0000-000000000099", file_id=file_id
                ),
                metadata=_bearer_metadata(token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


# ─── SharingService.CreateShareLink ────────────────────────────────────────────


async def test_grpc_create_share_link_denied_for_non_owner(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    stranger = await seed_user(db_pool, "stranger@example.com")
    folder = await seed_folder(db_pool, owner)
    stranger_token = create_access_token(stranger, "stranger@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SharingServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CreateShareLink(
                internal_pb2.CreateShareLinkRequest(folder_id=folder, permission="read"),
                metadata=_bearer_metadata(stranger_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_create_share_link_denied_for_write_grantee(grpc_server_address, db_pool):
    """Having write access to a folder doesn't grant the right to create share links for it."""
    owner = await seed_user(db_pool, "owner@example.com")
    grantee = await seed_user(db_pool, "grantee@example.com")
    folder = await seed_folder(db_pool, owner)
    await mdb.grant_folder_access(folder, grantee, "write", owner)
    grantee_token = create_access_token(grantee, "grantee@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SharingServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CreateShareLink(
                internal_pb2.CreateShareLinkRequest(folder_id=folder, permission="read"),
                metadata=_bearer_metadata(grantee_token),
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_create_share_link_allowed_for_owner(grpc_server_address, db_pool):
    owner = await seed_user(db_pool, "owner@example.com")
    folder = await seed_folder(db_pool, owner)
    token = create_access_token(owner, "owner@example.com")

    async with grpc.aio.insecure_channel(grpc_server_address) as channel:
        stub = internal_pb2_grpc.SharingServiceStub(channel)
        resp = await stub.CreateShareLink(
            internal_pb2.CreateShareLinkRequest(folder_id=folder, permission="read"),
            metadata=_bearer_metadata(token),
        )

    assert resp.token
    created_by = await db_pool.fetchval(
        "SELECT created_by::text FROM share_links WHERE token = $1", resp.token
    )
    assert created_by == owner


# ─── block_server gRPC: BlockServer service-key auth ───────────────────────────


async def test_grpc_upload_block_denied_without_service_key(
    block_grpc_server_address, monkeypatch
):
    monkeypatch.setattr(block_settings, "service_api_key", "test-secret-key")
    data = b"hello world"
    h = hashlib.sha256(data).hexdigest()

    async with grpc.aio.insecure_channel(block_grpc_server_address) as channel:
        stub = block_pb2_grpc.BlockServerStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.UploadBlock(
                block_pb2.UploadBlockRequest(hash=h, size=len(data), data=data)
            )

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_grpc_upload_block_allowed_with_correct_service_key(
    block_grpc_server_address, monkeypatch
):
    monkeypatch.setattr(block_settings, "service_api_key", "test-secret-key")
    data = b"hello world"
    h = hashlib.sha256(data).hexdigest()

    async with grpc.aio.insecure_channel(block_grpc_server_address) as channel:
        stub = block_pb2_grpc.BlockServerStub(channel)
        resp = await stub.UploadBlock(
            block_pb2.UploadBlockRequest(hash=h, size=len(data), data=data),
            metadata=(("x-service-key", "test-secret-key"),),
        )

    assert resp.stored is True


async def test_grpc_upload_block_allowed_when_service_key_unset(
    block_grpc_server_address,
):
    """Default settings.service_api_key is "" — enforcement disabled (dev/test mode)."""
    data = b"hello world unset key"
    h = hashlib.sha256(data).hexdigest()

    async with grpc.aio.insecure_channel(block_grpc_server_address) as channel:
        stub = block_pb2_grpc.BlockServerStub(channel)
        resp = await stub.UploadBlock(
            block_pb2.UploadBlockRequest(hash=h, size=len(data), data=data)
        )

    assert resp.stored is True


async def test_grpc_has_block_never_requires_service_key(
    block_grpc_server_address, monkeypatch
):
    monkeypatch.setattr(block_settings, "service_api_key", "test-secret-key")

    async with grpc.aio.insecure_channel(block_grpc_server_address) as channel:
        stub = block_pb2_grpc.BlockServerStub(channel)
        resp = await stub.HasBlock(block_pb2.HasBlockRequest(hash="0" * 64))

    assert resp.exists is False


async def test_grpc_get_block_denied_without_service_key(
    block_grpc_server_address, monkeypatch
):
    monkeypatch.setattr(block_settings, "service_api_key", "test-secret-key")

    async with grpc.aio.insecure_channel(block_grpc_server_address) as channel:
        stub = block_pb2_grpc.BlockServerStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            async for _ in stub.GetBlock(block_pb2.HasBlockRequest(hash="0" * 64)):
                pass

    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
