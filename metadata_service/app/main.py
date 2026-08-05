"""
Metadata Service — FastAPI application.

Production features:
  - Structured logging (structlog)
  - Request ID middleware (X-Request-ID)
  - JWT authentication on all protected routes
  - POST /auth/register and POST /auth/login
  - Health + readiness endpoints (/health, /ready)
  - Global exception handler
  - Input validation (UUID format, name length, limit bounds)
  - CORS middleware
  - Prometheus metrics

REST endpoints (from OpenAPI spec):
  POST   /directories               create_directory
  DELETE /files/{file_id}           soft-delete a file
  GET    /files/{file_id}/history   version history

Additional REST endpoints (required by sync loop):
  POST   /files                           create a file record
  POST   /files/{file_id}/versions        commit a new version
  POST   /folders/{folder_id}/shares      grant folder access
  GET    /share-links/{token}             validate a shareable link

Auth:
  POST   /auth/register    register a new user
  POST   /auth/login       obtain a Bearer JWT

WebSocket:
  WS /ws/{device_id}
    Client → Server: {"type": "subscribe",   "file_ids": [...]}
    Client → Server: {"type": "unsubscribe", "file_ids": [...]}
    Server → Client: {"type": "file_changed", ...}
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, field_validator

from . import db, notifier
from .auth import create_access_token, get_current_user, hash_password, verify_password
from .config import settings

# ─── Logging setup ────────────────────────────────────────────────────────────

logging.basicConfig(level=settings.log_level)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
)
log = structlog.get_logger()

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Metadata Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app)


# ─── Request ID middleware ─────────────────────────────────────────────────────


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID", str(uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=req_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    return response


# ─── Global error handler ─────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", error=str(exc), path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc)},
    )


# ─── Lifecycle ────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup() -> None:
    await db.init_db()
    await notifier.init_redis()
    asyncio.create_task(notifier.listener_task())
    log.info("metadata_service_started")


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close_db()
    await notifier.close_redis()


# ─── Request / Response models ────────────────────────────────────────────────

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


class CreateDirectoryRequest(BaseModel):
    parent_folder_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=255)


class CreateFileRequest(BaseModel):
    folder_id: str
    name: str = Field(..., min_length=1, max_length=255)


class CommitVersionRequest(BaseModel):
    device_id: Optional[str] = None
    block_hashes: List[str] = Field(..., min_length=1)

    @field_validator("block_hashes", mode="before")
    @classmethod
    def validate_hashes(cls, v):
        if isinstance(v, list):
            for item in v:
                if not _SHA256_RE.match(item):
                    raise ValueError(f"Invalid SHA-256 hash: {item!r}")
        return v


class GrantShareRequest(BaseModel):
    grantee_user_id: str
    permission: str = Field(..., pattern="^(read|write)$")
    granted_by: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _serialize(obj: dict) -> dict:
    return {
        k: v.isoformat() if isinstance(v, datetime) else v
        for k, v in obj.items()
    }


# ─── Auth endpoints (public) ──────────────────────────────────────────────────


@app.post("/auth/register", status_code=201)
async def register(req: RegisterRequest):
    pw_hash = hash_password(req.password)
    try:
        user_id = await db.register_user(req.email, pw_hash)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    log.info("user_registered", email=req.email)
    return {"user_id": user_id}


@app.post("/auth/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    user = await db.get_user_by_email(form.username)
    if not user or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(user["id"], user["email"])
    log.info("user_logged_in", email=form.username)
    return {"access_token": token, "token_type": "bearer"}


# ─── Health ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    db_ok = await db.ping_db()
    redis_ok = await notifier.ping_redis()
    if not db_ok or not redis_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "db": db_ok, "redis": redis_ok},
        )
    return {"status": "ok", "db": True, "redis": True}


# ─── REST — from OpenAPI spec (protected) ─────────────────────────────────────


@app.post("/directories", status_code=201)
async def create_directory(
    req: CreateDirectoryRequest,
    user: dict = Depends(get_current_user),
):
    if req.parent_folder_id:
        permission = await db.get_folder_permission(user["user_id"], req.parent_folder_id)
        if permission not in ("owner", "write"):
            raise HTTPException(
                status_code=403, detail="write access to parent_folder_id required"
            )
    folder_id = await db.create_directory(user["user_id"], req.parent_folder_id, req.name)
    log.info("directory_created", folder_id=folder_id)
    return {"folder_id": folder_id}


@app.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    user: dict = Depends(get_current_user),
):
    ok = await db.delete_file(user["user_id"], file_id)
    if not ok:
        raise HTTPException(status_code=404, detail="file not found or not owned by user")
    log.info("file_deleted", file_id=file_id)
    return {"ok": True}


@app.get("/files/{file_id}/history")
async def get_file_history(
    file_id: str,
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    folder_id = await db.get_file_folder_id(file_id)
    if folder_id is None:
        raise HTTPException(status_code=404, detail="file not found")
    permission = await db.get_folder_permission(user["user_id"], folder_id)
    if permission is None:
        raise HTTPException(status_code=403, detail="access to this file's folder required")
    versions = await db.get_file_history(file_id, limit)
    return {"versions": [_serialize(v) for v in versions]}


# ─── REST — sync loop endpoints (protected) ───────────────────────────────────


@app.post("/files", status_code=201)
async def create_file(
    req: CreateFileRequest,
    user: dict = Depends(get_current_user),
):
    permission = await db.get_folder_permission(user["user_id"], req.folder_id)
    if permission not in ("owner", "write"):
        raise HTTPException(status_code=403, detail="write access to folder_id required")
    file_id = await db.create_file(req.folder_id, req.name)
    log.info("file_created", file_id=file_id)
    return {"file_id": file_id}


@app.post("/files/{file_id}/versions", status_code=201)
async def commit_version(
    file_id: str,
    req: CommitVersionRequest,
    user: dict = Depends(get_current_user),
):
    folder_id = await db.get_file_folder_id(file_id)
    if folder_id is None:
        raise HTTPException(status_code=404, detail="file not found")
    permission = await db.get_folder_permission(user["user_id"], folder_id)
    if permission not in ("owner", "write"):
        raise HTTPException(
            status_code=403, detail="write access to this file's folder required"
        )

    try:
        result = await db.commit_file_version(
            file_id, req.device_id, user["user_id"], req.block_hashes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    timestamp = datetime.now(timezone.utc).isoformat()
    await notifier.publish_file_update(file_id, result["version_id"], timestamp)
    log.info("version_committed", file_id=file_id, version=result["version_number"])
    return result


@app.post("/folders/{folder_id}/shares", status_code=201)
async def grant_folder_access(
    folder_id: str,
    req: GrantShareRequest,
    user: dict = Depends(get_current_user),
):
    await db.grant_folder_access(
        folder_id, req.grantee_user_id, req.permission, req.granted_by
    )
    return {"ok": True}


@app.get("/share-links/{token}")
async def validate_share_link(token: str, user: dict = Depends(get_current_user)):
    result = await db.validate_share_link(token)
    if result is None:
        raise HTTPException(status_code=404, detail="share link not found or expired")
    return _serialize(result)


# ─── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws/{device_id}")
async def websocket_endpoint(websocket: WebSocket, device_id: str):
    """
    Real-time sync notification channel.

    Protocol (all JSON):
      Client → Server:
        {"type": "subscribe",   "file_ids": [...]}
        {"type": "unsubscribe", "file_ids": [...]}
      Server → Client:
        {"type": "subscribed",   "file_ids": [...]}
        {"type": "unsubscribed", "file_ids": [...]}
        {"type": "file_changed", "file_id": "...", "version_id": "...", "timestamp": "..."}
    """
    await websocket.accept()
    log.info("ws_connected", device_id=device_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"error": "invalid JSON"}))
                continue

            msg_type = msg.get("type")
            file_ids: List[str] = msg.get("file_ids", [])
            if not isinstance(file_ids, list):
                await websocket.send_text(json.dumps({"error": "file_ids must be a list"}))
                continue

            if msg_type == "subscribe":
                for fid in file_ids:
                    await notifier.subscribe_ws(fid, websocket)
                await websocket.send_text(
                    json.dumps({"type": "subscribed", "file_ids": file_ids})
                )
            elif msg_type == "unsubscribe":
                for fid in file_ids:
                    await notifier.unsubscribe_ws(fid, websocket)
                await websocket.send_text(
                    json.dumps({"type": "unsubscribed", "file_ids": file_ids})
                )
            else:
                await websocket.send_text(
                    json.dumps({"error": f"unknown message type: {msg_type!r}"})
                )
    except WebSocketDisconnect:
        log.info("ws_disconnected", device_id=device_id)
    except Exception as exc:
        log.error("ws_error", device_id=device_id, error=str(exc))
    finally:
        await notifier.unsubscribe_all(websocket)
