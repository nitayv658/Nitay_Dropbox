"""
PostgreSQL database operations for the Metadata Service.

Covers:
  - Auth:      register_user, get_user_by_email
  - Metadata:  create_directory, delete_file, get_file_history
  - Files/versions: create_file, commit_file_version
  - Sync:      get_missing_blocks, upsert_device_cursor, get_current_version_id
  - Sharing:   create_share_link, validate_share_link, grant_folder_access
"""

import secrets
from datetime import datetime, timezone, timedelta
from typing import List, Optional

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
    if pool:
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


# ─── Auth ─────────────────────────────────────────────────────────────────────


async def register_user(email: str, password_hash: str) -> str:
    """
    Insert a new user. Raises ValueError if the email is already taken.
    Returns the new user UUID.
    """
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (email, display_name, password_hash)
            VALUES ($1, $1, $2)
            RETURNING id::text
            """,
            email,
            password_hash,
        )
    except asyncpg.UniqueViolationError:
        raise ValueError(f"Email already registered: {email}")
    return row["id"]


async def get_user_by_email(email: str) -> Optional[dict]:
    """Look up a user by email. Returns {id, email, password_hash} or None."""
    row = await pool.fetchrow(
        "SELECT id::text, email, password_hash FROM users WHERE email = $1",
        email,
    )
    return dict(row) if row else None


# ─── Authorization ────────────────────────────────────────────────────────────


async def get_folder_permission(user_id: str, folder_id: str) -> Optional[str]:
    """
    Return the caller's access level for a folder: 'owner' (via folders.owner_id),
    the granted permission from folder_shares ('read'/'write'), or None if the
    folder doesn't exist or the user has neither ownership nor a share.
    """
    row = await pool.fetchrow(
        """
        SELECT CASE WHEN f.owner_id = $1::uuid THEN 'owner' ELSE fs.permission END AS permission
        FROM folders f
        LEFT JOIN folder_shares fs
            ON fs.folder_id = f.id AND fs.grantee_user_id = $1::uuid
        WHERE f.id = $2::uuid
        """,
        user_id,
        folder_id,
    )
    return row["permission"] if row else None


# ─── Metadata ─────────────────────────────────────────────────────────────────


async def create_directory(
    user_id: str, parent_folder_id: Optional[str], name: str
) -> str:
    """Create a folder owned by user_id. Returns the new folder UUID."""
    row = await pool.fetchrow(
        """
        INSERT INTO folders (owner_id, parent_id, name)
        VALUES ($1::uuid, $2::uuid, $3)
        RETURNING id::text
        """,
        user_id,
        parent_folder_id,
        name,
    )
    return row["id"]


async def delete_file(user_id: str, file_id: str) -> bool:
    """
    Soft-delete a file. The owning folder must belong to user_id.
    Returns True if found and deleted, False otherwise.
    """
    row = await pool.fetchrow(
        """
        UPDATE files f
        SET deleted = TRUE
        FROM folders fo
        WHERE f.id = $1::uuid
          AND f.folder_id = fo.id
          AND fo.owner_id = $2::uuid
          AND f.deleted = FALSE
        RETURNING f.id
        """,
        file_id,
        user_id,
    )
    return row is not None


async def get_file_history(file_id: str, limit: int) -> List[dict]:
    """Return file versions ordered newest-first, up to `limit` entries."""
    rows = await pool.fetch(
        """
        SELECT
            id::text,
            version_number,
            created_by_device::text,
            created_by_user::text,
            created_at,
            size
        FROM file_versions
        WHERE file_id = $1::uuid
        ORDER BY version_number DESC
        LIMIT $2
        """,
        file_id,
        limit,
    )
    return [dict(r) for r in rows]


# ─── File / Version ───────────────────────────────────────────────────────────


async def create_file(user_id: str, folder_id: str, name: str) -> str:
    """Insert a new file record (no version yet). Returns the file UUID."""
    row = await pool.fetchrow(
        """
        INSERT INTO files (folder_id, name, size)
        VALUES ($1::uuid, $2, 0)
        RETURNING id::text
        """,
        folder_id,
        name,
    )
    return row["id"]


async def commit_file_version(
    file_id: str,
    device_id: Optional[str],
    user_id: str,
    block_hashes: List[str],
) -> dict:
    """
    Atomically create a new file version and link its blocks.

    Steps:
      1. Row-lock the file to serialise concurrent commits (SELECT FOR UPDATE).
      2. Determine next version_number.
      3. INSERT file_versions row.
      4. For each block hash (in order): validate, INSERT file_version_blocks.
      5. UPDATE files.current_version_id and size.
      6. Increment blocks.ref_count for all referenced hashes.
      7. Persist total size on the version row.

    Raises ValueError if any block hash is unknown to the block server DB.
    Returns {"version_id": str, "version_number": int}.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the file row so concurrent commits for the same file are
            # serialized, preventing duplicate version_number values.
            await conn.execute(
                "SELECT id FROM files WHERE id = $1::uuid FOR UPDATE", file_id
            )
            max_ver = await conn.fetchval(
                "SELECT COALESCE(MAX(version_number), 0) FROM file_versions WHERE file_id = $1::uuid",
                file_id,
            )
            next_ver = max_ver + 1

            version_id = await conn.fetchval(
                """
                INSERT INTO file_versions
                    (file_id, version_number, created_by_device, created_by_user, size)
                VALUES ($1::uuid, $2, $3::uuid, $4::uuid, 0)
                RETURNING id::text
                """,
                file_id,
                next_ver,
                device_id or None,
                user_id,
            )

            total_size = 0
            for idx, hash_hex in enumerate(block_hashes):
                block = await conn.fetchrow(
                    "SELECT size FROM blocks WHERE hash = $1", hash_hex
                )
                if block is None:
                    raise ValueError(
                        f"Block '{hash_hex}' not found; upload it to the Block Server first"
                    )
                block_size = block["size"]
                total_size += block_size

                await conn.execute(
                    """
                    INSERT INTO file_version_blocks
                        (file_version_id, block_hash, block_index, block_size)
                    VALUES ($1::uuid, $2, $3, $4)
                    """,
                    version_id,
                    hash_hex,
                    idx,
                    block_size,
                )

            await conn.execute(
                """
                UPDATE files
                SET current_version_id = $1::uuid, size = $2
                WHERE id = $3::uuid
                """,
                version_id,
                total_size,
                file_id,
            )

            if block_hashes:
                await conn.executemany(
                    "UPDATE blocks SET ref_count = ref_count + 1 WHERE hash = $1",
                    [(h,) for h in block_hashes],
                )

            await conn.execute(
                "UPDATE file_versions SET size = $1 WHERE id = $2::uuid",
                total_size,
                version_id,
            )

    return {"version_id": version_id, "version_number": next_ver}


async def get_current_version_id(file_id: str) -> Optional[str]:
    """Return the UUID of the file's current version, or None."""
    row = await pool.fetchrow(
        "SELECT current_version_id::text FROM files WHERE id = $1::uuid AND deleted = FALSE",
        file_id,
    )
    return row["current_version_id"] if row else None


# ─── Sync ─────────────────────────────────────────────────────────────────────


async def get_missing_blocks(
    device_id: str, file_id: str, from_version_id: Optional[str]
) -> List[str]:
    """
    Return hashes of blocks in the file's current version that are absent
    from the device's last-known version (from_version_id).

    If from_version_id is None the device is treated as fresh and all blocks
    in the current version are returned. Results are ordered by block_index.
    """
    if from_version_id:
        rows = await pool.fetch(
            """
            WITH current AS (
                SELECT fvb.block_hash, fvb.block_index
                FROM file_version_blocks fvb
                JOIN files f ON f.current_version_id = fvb.file_version_id
                WHERE f.id = $1::uuid AND f.deleted = FALSE
            ),
            known AS (
                SELECT fvb.block_hash
                FROM file_version_blocks fvb
                WHERE fvb.file_version_id = $2::uuid
            )
            SELECT c.block_hash
            FROM current c
            WHERE c.block_hash NOT IN (SELECT block_hash FROM known)
            ORDER BY c.block_index
            """,
            file_id,
            from_version_id,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT fvb.block_hash
            FROM file_version_blocks fvb
            JOIN files f ON f.current_version_id = fvb.file_version_id
            WHERE f.id = $1::uuid AND f.deleted = FALSE
            ORDER BY fvb.block_index
            """,
            file_id,
        )
    return [r["block_hash"] for r in rows]


async def upsert_device_cursor(
    device_id: str, file_id: str, version_id: str
) -> None:
    """Advance (or create) the device cursor to version_id for the given file."""
    await pool.execute(
        """
        INSERT INTO device_cursors (device_id, file_id, last_seen_version, updated_at)
        VALUES ($1::uuid, $2::uuid, $3::uuid, now())
        ON CONFLICT (device_id, file_id)
        DO UPDATE SET last_seen_version = EXCLUDED.last_seen_version,
                      updated_at        = now()
        """,
        device_id,
        file_id,
        version_id,
    )


# ─── Sharing ──────────────────────────────────────────────────────────────────


async def create_share_link(
    folder_id: str,
    created_by: Optional[str],
    permission: str,
    ttl_seconds: Optional[int],
) -> dict:
    """
    Generate a cryptographically random token and store a share link.
    Returns {"token": str, "expires_at": ISO-string or None}.
    """
    token = secrets.token_urlsafe(32)
    expires_at = None
    if ttl_seconds and ttl_seconds > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    await pool.execute(
        """
        INSERT INTO share_links (folder_id, token, permission, expires_at, created_by)
        VALUES ($1::uuid, $2, $3, $4, $5::uuid)
        """,
        folder_id,
        token,
        permission,
        expires_at,
        created_by,
    )
    return {
        "token": token,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


async def validate_share_link(token: str) -> Optional[dict]:
    """
    Look up a share link by token.
    Returns {folder_id, permission, expires_at} if valid, None if not found or expired.
    """
    row = await pool.fetchrow(
        """
        SELECT folder_id::text, permission, expires_at
        FROM share_links
        WHERE token = $1
          AND (expires_at IS NULL OR expires_at > now())
        """,
        token,
    )
    return dict(row) if row else None


async def grant_folder_access(
    folder_id: str,
    grantee_user_id: str,
    permission: str,
    granted_by: str,
) -> None:
    """
    Grant (or upgrade) a user's access to a folder.
    Uses ON CONFLICT to update the permission if a share already exists.
    """
    await pool.execute(
        """
        INSERT INTO folder_shares (folder_id, grantee_user_id, permission, created_by)
        VALUES ($1::uuid, $2::uuid, $3, $4::uuid)
        ON CONFLICT (folder_id, grantee_user_id)
        DO UPDATE SET permission = EXCLUDED.permission
        """,
        folder_id,
        grantee_user_id,
        permission,
        granted_by,
    )
