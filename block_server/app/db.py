"""
Block Server — PostgreSQL operations via asyncpg.
"""

from typing import List, Optional, Tuple

import asyncpg

from .config import settings

pool: Optional[asyncpg.pool.Pool] = None


async def init_db() -> None:
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=settings.db_command_timeout,
        )


async def close_db() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def ping_db() -> bool:
    """Return True if the database is reachable, False otherwise."""
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


async def get_block(hash_: str) -> Optional[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT hash, size, s3_key, ref_count FROM blocks WHERE hash = $1",
            hash_,
        )


async def insert_block(hash_: str, size: int, s3_key: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO blocks(hash, size, s3_key, ref_count, created_at)
            VALUES ($1, $2, $3, 1, now())
            ON CONFLICT (hash) DO UPDATE SET ref_count = blocks.ref_count + 1
            """,
            hash_,
            size,
            s3_key,
        )


async def increment_refcount(hash_: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE blocks SET ref_count = ref_count + 1 WHERE hash = $1", hash_
        )


async def decrement_refcount(hash_: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE blocks SET ref_count = GREATEST(ref_count - 1, 0) WHERE hash = $1",
            hash_,
        )


async def delete_zero_refcount_blocks() -> List[Tuple[str, str]]:
    """
    Delete all blocks with ref_count = 0 and return their (hash, s3_key) pairs
    so the caller can also remove them from physical storage.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "DELETE FROM blocks WHERE ref_count = 0 RETURNING hash, s3_key"
        )
    return [(r["hash"], r["s3_key"]) for r in rows]
