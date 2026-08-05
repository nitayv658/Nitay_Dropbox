"""
gRPC Authentication Tests.

Verifies JWTAuthInterceptor rejects calls without a valid Bearer JWT before
they reach any servicer method, and passes authenticated calls through.
Uses a real in-process grpc.aio server + channel (grpc_server_address fixture
in conftest.py) — no mocking of the gRPC transport itself.
"""

import grpc
import pytest

from metadata_service.app import internal_pb2, internal_pb2_grpc
from metadata_service.app.auth import create_access_token
from tests.conftest import seed_user


def _bearer_metadata(token: str):
    return (("authorization", f"Bearer {token}"),)


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
