"""
Block Server — FastAPI application.

Production features:
  - Structured logging (structlog)
  - Request ID middleware (X-Request-ID header)
  - Health + readiness endpoints (/health, /ready)
  - Global exception handler with consistent error responses
  - Input validation (SHA-256 hash format, size bounds)
  - Service-to-service API key enforcement (optional, skipped when SERVICE_API_KEY unset)
  - Prometheus metrics via prometheus-fastapi-instrumentator
  - Hourly block GC task (deletes blocks with ref_count = 0)
  - CORS middleware
"""

import asyncio
import hashlib
import logging
import re
from uuid import uuid4

import structlog
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from . import db, storage
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

app = FastAPI(title="Block Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
async def startup():
    await db.init_db()
    asyncio.create_task(_gc_loop())
    log.info("block_server_started", storage_backend=settings.storage_backend)


@app.on_event("shutdown")
async def shutdown():
    await db.close_db()


# ─── Block GC background task ─────────────────────────────────────────────────


async def _gc_loop() -> None:
    """Hourly task: delete blocks with ref_count = 0 from DB and storage."""
    while True:
        await asyncio.sleep(settings.gc_interval_seconds)
        try:
            deleted = await db.delete_zero_refcount_blocks()
            for hash_hex, _ in deleted:
                try:
                    storage.delete_block(hash_hex)
                except Exception as e:
                    log.warning("gc_storage_delete_failed", hash=hash_hex, error=str(e))
            log.info("gc_complete", deleted_count=len(deleted))
        except Exception as e:
            log.error("gc_error", error=str(e))


# ─── Input validation helpers ─────────────────────────────────────────────────

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_hash(hash_: str) -> None:
    if not _SHA256_RE.match(hash_):
        raise HTTPException(
            status_code=400,
            detail="hash must be a 64-character lowercase hex string (SHA-256)",
        )


# ─── Service-to-service auth ──────────────────────────────────────────────────


async def _check_service_key(x_service_key: str = Header(None)) -> None:
    """Skip enforcement when SERVICE_API_KEY is not configured (dev/test)."""
    if not settings.service_api_key:
        return
    if x_service_key != settings.service_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing service API key")


# ─── Health ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    db_ok = await db.ping_db()
    if not db_ok:
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": False})
    return {"status": "ok", "db": True}


# ─── Block endpoints ──────────────────────────────────────────────────────────


@app.post("/blocks/upload", dependencies=[Depends(_check_service_key)])
async def upload_block(
    hash: str,
    size: int,
    file: UploadFile = File(...),
):
    _validate_hash(hash)

    if size <= 0 or size > settings.block_max_size:
        raise HTTPException(
            status_code=400,
            detail=f"size must be between 1 and {settings.block_max_size} bytes",
        )

    data = await file.read()

    # Verify hash integrity
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != hash:
        raise HTTPException(status_code=400, detail="hash mismatch: data does not match declared hash")

    existing = await db.get_block(hash)
    if existing:
        await db.increment_refcount(hash)
        log.info("block_deduped", hash=hash[:16])
        return JSONResponse({"stored": False, "s3_key": existing["s3_key"]})

    s3_key = storage.save_block(hash, data)
    await db.insert_block(hash, len(data), s3_key)
    log.info("block_stored", hash=hash[:16], size=len(data))
    return JSONResponse({"stored": True, "s3_key": s3_key})


@app.get("/blocks/has/{hash}")
async def has_block(hash: str):
    _validate_hash(hash)
    row = await db.get_block(hash)
    if not row:
        return JSONResponse({"exists": False, "s3_key": ""})
    return JSONResponse({"exists": True, "s3_key": row["s3_key"]})


@app.get("/blocks/get/{hash}", dependencies=[Depends(_check_service_key)])
async def get_block(hash: str):
    _validate_hash(hash)
    row = await db.get_block(hash)
    if not row:
        raise HTTPException(status_code=404, detail="block not found")
    try:
        _, path = storage.read_block(hash)
    except FileNotFoundError:
        log.error("block_missing_from_storage", hash=hash[:16])
        raise HTTPException(status_code=500, detail="block missing from storage")
    return FileResponse(path, media_type="application/octet-stream")
