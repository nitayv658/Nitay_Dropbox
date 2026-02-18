"""
Redis pub/sub notifier for real-time WebSocket sync notifications.

Design:
  - On file version commit, publish a JSON event to Redis channel "file:{file_id}".
  - A long-running background task (listener_task) subscribes to "file:*"
    and forwards each message to all in-process WebSocket connections watching that file.
  - WebSocket connections register/deregister via subscribe_ws / unsubscribe_ws.
"""

import asyncio
import json
from collections import defaultdict
from typing import Dict, Optional, Set

import aioredis
import structlog
from fastapi import WebSocket

from .config import settings

log = structlog.get_logger()

redis: Optional[aioredis.Redis] = None

# Maps file_id → set of WebSocket objects subscribed to that file
_connections: Dict[str, Set[WebSocket]] = defaultdict(set)
_lock = asyncio.Lock()


async def init_redis() -> None:
    global redis
    try:
        redis = await aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        log.info("redis_connected", url=settings.redis_url)
    except Exception as exc:
        log.error("redis_connect_failed", error=str(exc))


async def close_redis() -> None:
    global redis
    if redis:
        await redis.close()
        redis = None


async def ping_redis() -> bool:
    """Return True if Redis is reachable, False otherwise."""
    if redis is None:
        return False
    try:
        await redis.ping()
        return True
    except Exception:
        return False


# ─── Publisher ────────────────────────────────────────────────────────────────


async def publish_file_update(file_id: str, version_id: str, timestamp: str) -> None:
    """
    Publish a file-changed event so all devices subscribed to this file receive a push.
    No-op if Redis is not initialised (graceful degradation).
    """
    if redis is None:
        log.warning("redis_not_available_skipping_publish", file_id=file_id)
        return
    payload = json.dumps(
        {
            "type": "file_changed",
            "file_id": file_id,
            "version_id": version_id,
            "timestamp": timestamp,
        }
    )
    try:
        await redis.publish(f"file:{file_id}", payload)
    except Exception as exc:
        log.error("redis_publish_failed", file_id=file_id, error=str(exc))


# ─── Subscription registry ────────────────────────────────────────────────────


async def subscribe_ws(file_id: str, ws: WebSocket) -> None:
    async with _lock:
        _connections[file_id].add(ws)
    log.debug("ws_subscribed", file_id=file_id)


async def unsubscribe_ws(file_id: str, ws: WebSocket) -> None:
    async with _lock:
        _connections[file_id].discard(ws)
        if not _connections[file_id]:
            del _connections[file_id]


async def unsubscribe_all(ws: WebSocket) -> None:
    async with _lock:
        empty = [fid for fid, conns in _connections.items() if ws in conns]
        for fid in empty:
            _connections[fid].discard(ws)
            if not _connections[fid]:
                del _connections[fid]


# ─── Background listener ──────────────────────────────────────────────────────


async def _push(ws: WebSocket, message: str) -> None:
    try:
        await ws.send_text(message)
    except Exception:
        pass  # Client disconnected; cleanup happens when its handler exits


async def listener_task() -> None:
    """
    Long-running background task.
    Subscribes to Redis pattern "file:*" and forwards messages to matching WebSockets.
    Should be started with asyncio.create_task() on application startup.
    """
    if redis is None:
        log.warning("redis_not_initialised_ws_notifications_disabled")
        return

    pubsub = redis.pubsub()
    try:
        await pubsub.psubscribe("file:*")
        log.info("redis_listener_started")

        async for message in pubsub.listen():
            if message["type"] != "pmessage":
                continue
            channel: str = message["channel"]  # e.g. "file:<uuid>"
            parts = channel.split(":", 1)
            if len(parts) != 2:
                continue
            file_id = parts[1]
            payload: str = message["data"]

            async with _lock:
                sockets = list(_connections.get(file_id, []))

            if sockets:
                await asyncio.gather(*[_push(ws, payload) for ws in sockets])

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.error("redis_listener_error", error=str(exc))
    finally:
        try:
            await pubsub.punsubscribe("file:*")
        except Exception:
            pass
        log.info("redis_listener_stopped")
