"""
Tests for block_server's gRPC BlockServer service.
"""

import hashlib

import grpc

from proto import internal_pb2 as pb2
from proto import internal_pb2_grpc as pb2_grpc


async def test_get_block_returns_uploaded_data(block_grpc_server_address):
    data = b"hello from grpc block server"
    hash_hex = hashlib.sha256(data).hexdigest()

    async with grpc.aio.insecure_channel(block_grpc_server_address) as channel:
        stub = pb2_grpc.BlockServerStub(channel)

        upload_resp = await stub.UploadBlock(
            pb2.UploadBlockRequest(hash=hash_hex, size=len(data), data=data)
        )
        assert upload_resp.stored is True

        chunks = [
            resp
            async for resp in stub.GetBlock(pb2.HasBlockRequest(hash=hash_hex))
        ]

    assert len(chunks) == 1
    assert chunks[0].data == data
