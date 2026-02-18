"""
gRPC server for the Metadata Service.

Exposes three gRPC services defined in proto/internal.proto:
  - MetadataService  — CreateDirectory, DeleteFile, GetFileHistory
  - SyncService      — GetMissingBlocks (also advances the device cursor)
  - SharingService   — CreateShareLink

Prerequisites
-------------
Generate Python stubs from the project root before running:

    python -m grpc_tools.protoc \\
        -I proto \\
        --python_out=metadata_service/app \\
        --grpc_python_out=metadata_service/app \\
        proto/internal.proto

Run
---
    python -m metadata_service.app.grpc_server
"""

import asyncio

import grpc
import structlog
from google.protobuf.timestamp_pb2 import Timestamp

from . import db
from . import internal_pb2, internal_pb2_grpc
from .config import settings

log = structlog.get_logger()


def _to_proto_timestamp(dt) -> Timestamp:
    ts = Timestamp()
    if dt is not None:
        ts.FromDatetime(dt)
    return ts


# ─── MetadataService ──────────────────────────────────────────────────────────


class MetadataServiceServicer(internal_pb2_grpc.MetadataServiceServicer):

    async def CreateDirectory(self, request, context):
        if not request.name:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name is required")
        try:
            folder_id = await db.create_directory(
                request.user_id,
                request.parent_folder_id or None,
                request.name,
            )
            log.info("grpc_create_directory", folder_id=folder_id)
            return internal_pb2.CreateDirectoryResponse(folder_id=folder_id)
        except Exception as exc:
            log.error("grpc_create_directory_error", error=str(exc))
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    async def DeleteFile(self, request, context):
        try:
            ok = await db.delete_file(request.user_id, request.file_id)
            if not ok:
                await context.abort(
                    grpc.StatusCode.NOT_FOUND, "file not found or not owned by user"
                )
            log.info("grpc_delete_file", file_id=request.file_id)
            return internal_pb2.DeleteFileResponse(ok=ok)
        except Exception as exc:
            log.error("grpc_delete_file_error", error=str(exc))
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    async def GetFileHistory(self, request, context):
        limit = max(1, min(request.limit if request.limit > 0 else 50, 200))
        try:
            rows = await db.get_file_history(request.file_id, limit)
            versions = [
                internal_pb2.FileVersion(
                    id=r["id"],
                    version_number=r["version_number"],
                    created_by_device=r.get("created_by_device") or "",
                    created_by_user=r.get("created_by_user") or "",
                    created_at=_to_proto_timestamp(r.get("created_at")),
                    size=r["size"],
                )
                for r in rows
            ]
            return internal_pb2.FileHistoryResponse(versions=versions)
        except Exception as exc:
            log.error("grpc_get_file_history_error", error=str(exc))
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))


# ─── SyncService ──────────────────────────────────────────────────────────────


class SyncServiceServicer(internal_pb2_grpc.SyncServiceServicer):

    async def GetMissingBlocks(self, request, context):
        from_version = request.from_version_id or None
        try:
            missing = await db.get_missing_blocks(
                request.device_id, request.file_id, from_version
            )
            current_version_id = await db.get_current_version_id(request.file_id)
            if current_version_id:
                await db.upsert_device_cursor(
                    request.device_id, request.file_id, current_version_id
                )
            log.info(
                "grpc_get_missing_blocks",
                device_id=request.device_id,
                missing_count=len(missing),
            )
            return internal_pb2.MissingBlocksResponse(missing_block_hashes=missing)
        except Exception as exc:
            log.error("grpc_get_missing_blocks_error", error=str(exc))
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))


# ─── SharingService ───────────────────────────────────────────────────────────


class SharingServiceServicer(internal_pb2_grpc.SharingServiceServicer):

    async def CreateShareLink(self, request, context):
        if request.permission not in ("read", "write"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "permission must be 'read' or 'write'"
            )
        ttl = request.ttl_seconds if request.ttl_seconds > 0 else None
        try:
            result = await db.create_share_link(
                folder_id=request.folder_id,
                created_by=None,
                permission=request.permission,
                ttl_seconds=ttl,
            )
            log.info("grpc_create_share_link", folder_id=request.folder_id)
            return internal_pb2.CreateShareLinkResponse(
                token=result["token"],
                expires_at=result["expires_at"] or "",
            )
        except Exception as exc:
            log.error("grpc_create_share_link_error", error=str(exc))
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))


# ─── Server bootstrap ─────────────────────────────────────────────────────────


async def serve() -> None:
    await db.init_db()

    server = grpc.aio.server()

    internal_pb2_grpc.add_MetadataServiceServicer_to_server(
        MetadataServiceServicer(), server
    )
    internal_pb2_grpc.add_SyncServiceServicer_to_server(
        SyncServiceServicer(), server
    )
    internal_pb2_grpc.add_SharingServiceServicer_to_server(
        SharingServiceServicer(), server
    )

    listen_addr = f"0.0.0.0:{settings.grpc_port}"
    server.add_insecure_port(listen_addr)
    log.info("grpc_server_started", address=listen_addr)

    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level="INFO")
    asyncio.run(serve())
