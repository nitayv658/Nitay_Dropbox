"""
Shared pytest fixtures for the Dropbox-sim test suite.

Prerequisites:
  - A running PostgreSQL instance with a `dropboxsim_test` database.
  - Set TEST_DATABASE_URL env var to override the default connection string.

The session-scoped db_pool wipes and re-applies the schema once per test run.
The autouse clean_tables fixture truncates all rows before every individual test,
giving each test a perfectly clean slate without the overhead of schema recreation.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ─── Config ───────────────────────────────────────────────────────────────────

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/dropboxsim_test",
)

_SCHEMA_SQL = (
    Path(__file__).parent.parent / "schema" / "sql" / "schema.sql"
).read_text()

_REPO_ROOT = Path(__file__).parent.parent


def _generate_grpc_stubs(out_dir: str) -> None:
    subprocess.run(
        [
            sys.executable, "-m", "grpc_tools.protoc",
            "-I", "proto",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            "proto/internal.proto",
        ],
        cwd=_REPO_ROOT,
        check=True,
    )
    grpc_stub = _REPO_ROOT / out_dir / "internal_pb2_grpc.py"
    text = grpc_stub.read_text()
    grpc_stub.write_text(
        text.replace(
            "import internal_pb2 as internal__pb2",
            "from . import internal_pb2 as internal__pb2",
        )
    )


def _ensure_grpc_stubs() -> None:
    """
    Generate internal_pb2*.py from proto/internal.proto into both locations
    the gRPC servers import them from (metadata_service/app/ and proto/
    itself — block_server/app/grpc_server.py does `from proto import ...`)
    if they're not already present, so gRPC test modules can import them at
    collection time. Mirrors `make generate-proto`, including the sed-style
    patch for grpc_tools' flat `import internal_pb2` (see Makefile).
    """
    if not (_REPO_ROOT / "metadata_service" / "app" / "internal_pb2_grpc.py").exists():
        _generate_grpc_stubs("metadata_service/app")
    if not (_REPO_ROOT / "proto" / "internal_pb2_grpc.py").exists():
        _generate_grpc_stubs("proto")


_ensure_grpc_stubs()


# ─── Database pool ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL)

    # Reset and re-apply schema once for the whole test session
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(_SCHEMA_SQL)

    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(db_pool):
    """Truncate all data before every test for a clean slate."""
    async with db_pool.acquire() as conn:
        # blocks has no FK to users; truncate both roots with CASCADE
        await conn.execute("TRUNCATE users, blocks CASCADE")
    yield


# ─── Storage mock ─────────────────────────────────────────────────────────────


@pytest.fixture
def in_memory_storage():
    """
    Replace the filesystem block storage with an in-memory dict.
    Tests receive the dict directly so they can assert len(store) == N.
    """
    store: dict[str, bytes] = {}

    def _save(hash_hex: str, data: bytes) -> str:
        store[hash_hex] = data
        return f"mem://{hash_hex}"

    def _read(hash_hex: str):
        if hash_hex not in store:
            raise FileNotFoundError(hash_hex)
        return store[hash_hex], f"mem://{hash_hex}"

    def _exists(hash_hex: str):
        ok = hash_hex in store
        return ok, (f"mem://{hash_hex}" if ok else "")

    with (
        patch("block_server.app.storage.save_block", side_effect=_save),
        patch("block_server.app.storage.read_block", side_effect=_read),
        patch("block_server.app.storage.exists_block", side_effect=_exists),
    ):
        yield store


# ─── HTTP test clients ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def block_client(db_pool, in_memory_storage):
    """
    Async HTTP client wired directly to the Block Server FastAPI app.
    Uses the shared test pool; startup/shutdown events are suppressed so
    the test pool is never closed between tests.
    """
    import block_server.app.db as bdb

    bdb.pool = db_pool  # init_db will skip (pool is not None)

    with patch.object(bdb, "close_db", new=AsyncMock()):
        from block_server.app.main import app as block_app

        async with AsyncClient(
            transport=ASGITransport(app=block_app), base_url="http://test"
        ) as client:
            yield client


@pytest_asyncio.fixture
async def meta_client(db_pool):
    """
    Async HTTP client wired to the Metadata Service FastAPI app.
    Redis notifier is fully mocked so tests don't require a running Redis.
    JWT auth dependency is overridden to return a test user so tests don't
    need real Bearer tokens.
    """
    import metadata_service.app.db as mdb

    mdb.pool = db_pool

    with (
        patch.object(mdb, "close_db", new=AsyncMock()),
        patch("metadata_service.app.notifier.init_redis", new=AsyncMock()),
        patch("metadata_service.app.notifier.close_redis", new=AsyncMock()),
        patch("metadata_service.app.notifier.listener_task", new=AsyncMock()),
        patch("metadata_service.app.notifier.publish_file_update", new=AsyncMock()),
        patch("metadata_service.app.notifier.ping_redis", new=AsyncMock(return_value=True)),
    ):
        from metadata_service.app.auth import get_current_user
        from metadata_service.app.main import app as meta_app

        # Override JWT auth — tests don't need real tokens
        meta_app.dependency_overrides[get_current_user] = lambda: {
            "user_id": "00000000-0000-0000-0000-000000000001",
            "email": "test@test.com",
        }

        async with AsyncClient(
            transport=ASGITransport(app=meta_app), base_url="http://test"
        ) as client:
            yield client

        meta_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def override_user(meta_client):
    """
    Callable fixture: override_user(user_id) swaps the identity meta_client's
    requests authenticate as, so tests can simulate a second, non-default user
    (e.g. an attacker without access to another user's resources).
    Restored automatically by meta_client's teardown.
    """
    from metadata_service.app.auth import get_current_user
    from metadata_service.app.main import app as meta_app

    def _set(user_id: str, email: str = "other@test.com"):
        meta_app.dependency_overrides[get_current_user] = lambda: {
            "user_id": user_id,
            "email": email,
        }

    return _set


# ─── gRPC test server ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def block_grpc_server_address(db_pool, in_memory_storage):
    """
    Starts block_server's gRPC server on an ephemeral localhost port, wired to
    the shared test db pool and in-memory storage. Yields the "host:port"
    address for a test client channel.
    """
    import grpc

    import block_server.app.db as bdb
    from block_server.app.grpc_server import BlockServerServicer
    from proto import internal_pb2_grpc as block_pb2_grpc

    bdb.pool = db_pool

    server = grpc.aio.server()
    block_pb2_grpc.add_BlockServerServicer_to_server(BlockServerServicer(), server)

    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(None)


# ─── Seed helpers (plain async functions — call from tests directly) ──────────


async def seed_user(pool: asyncpg.pool.Pool, email: str = "test@example.com") -> str:
    return await pool.fetchval(
        "INSERT INTO users (email, display_name) VALUES ($1, $2) RETURNING id::text",
        email,
        email.split("@")[0],
    )


async def seed_folder(
    pool: asyncpg.pool.Pool, owner_id: str, name: str = "root"
) -> str:
    return await pool.fetchval(
        "INSERT INTO folders (owner_id, name) VALUES ($1::uuid, $2) RETURNING id::text",
        owner_id,
        name,
    )
