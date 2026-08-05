import asyncio
import hashlib
from concurrent import futures

import grpc
from grpc import aio

from proto import internal_pb2 as pb2
from proto import internal_pb2_grpc as pb2_grpc

from . import db, storage
from .grpc_auth import ServiceKeyInterceptor


class BlockServerServicer(pb2_grpc.BlockServerServicer):
    async def UploadBlock(self, request, context):
        # request: hash, size, data
        hash_hex = request.hash
        size = request.size
        data = request.data

        if len(data) != size:
            # allow but warn (could set metadata)
            pass

        h = hashlib.sha256(data).hexdigest()
        if h != hash_hex:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('hash mismatch')
            return pb2.UploadBlockResponse(stored=False, s3_key="")

        row = await db.get_block(hash_hex)
        if row:
            await db.increment_refcount(hash_hex)
            return pb2.UploadBlockResponse(stored=False, s3_key=row['s3_key'])

        s3_key = storage.save_block(hash_hex, data)
        await db.insert_block(hash_hex, size, s3_key)
        return pb2.UploadBlockResponse(stored=True, s3_key=s3_key)

    async def HasBlock(self, request, context):
        hash_hex = request.hash
        row = await db.get_block(hash_hex)
        if not row:
            return pb2.HasBlockResponse(exists=False, s3_key="")
        return pb2.HasBlockResponse(exists=True, s3_key=row['s3_key'])

    async def GetBlock(self, request, context):
        hash_hex = request.hash
        row = await db.get_block(hash_hex)
        if not row:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details('block not found')
            return
        try:
            data, path = storage.read_block(hash_hex)
        except FileNotFoundError:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('block missing on storage')
            return
        # stream as single response (proto defines stream of UploadBlockResponse)
        yield pb2.UploadBlockResponse(stored=True, s3_key=path, data=data)


async def serve(host='0.0.0.0', port=50051):
    server = aio.server(interceptors=[ServiceKeyInterceptor()])
    pb2_grpc.add_BlockServerServicer_to_server(BlockServerServicer(), server)
    server.add_insecure_port(f"{host}:{port}")
    await db.init_db()
    await server.start()
    print(f"gRPC BlockServer listening on {host}:{port}")
    await server.wait_for_termination()


if __name__ == '__main__':
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
