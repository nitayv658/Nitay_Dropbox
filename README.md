# Dropbox-sim — Simplified Dropbox-like Backend

## Overview
A production-minded, simplified backend implementing block-based file sync, metadata/versioning, device cursors for delta sync, and shareable links.

### Components
| Component | Description |
|---|---|
| **Block Server** | Stores deduplicated 4 MB blocks (content-addressed by SHA-256). Upload / exists / get operations via HTTP and gRPC. |
| **Metadata Service** | Tracks users, devices, folders, files, and versions. Maps each file version to an ordered list of block hashes. |
| **Sync Engine** | Computes missing blocks per device cursor (set-difference query). Pushes real-time notifications via Redis → WebSocket. |
| **Sharing Module** | Folder-scoped permissions (`read`/`write`/`owner`) and token-based shareable links with optional TTL. |
| **File Chunker** | Client-side utility that splits files into 4 MB blocks, hashes them, and uploads concurrently with a configurable semaphore. |

---

## Tech Stack
| Layer | Technology |
|---|---|
| Language | Python 3.10+ / FastAPI |
| Database | PostgreSQL (asyncpg) |
| Cache / Queue | Redis (aioredis pub/sub) |
| Object Storage | Local filesystem (mock S3 — swap `storage.py` for real S3) |
| Internal comms | gRPC (grpcio) |
| Real-time | WebSockets (uvicorn) |
| Tests | pytest + pytest-asyncio + httpx |

---

## Repository Layout
```
Nitay_Dropbox/
├── block_server/               # Block Server service
│   ├── app/
│   │   ├── main.py             # FastAPI HTTP endpoints
│   │   ├── grpc_server.py      # gRPC BlockServer implementation
│   │   ├── db.py               # asyncpg — blocks table operations
│   │   └── storage.py          # Local filesystem mock (S3 interface)
│   └── requirements.txt
│
├── metadata_service/           # Metadata + Sync + Sharing service
│   ├── app/
│   │   ├── main.py             # FastAPI HTTP + WebSocket endpoints
│   │   ├── grpc_server.py      # gRPC MetadataService / SyncService / SharingService
│   │   ├── db.py               # asyncpg — all metadata/sync/sharing operations
│   │   └── notifier.py         # Redis pub/sub → WebSocket fan-out
│   ├── requirements.txt
│   └── README.md               # Setup & run instructions
│
├── client_utils/
│   └── chunker.py              # Async file chunker + concurrent block uploader
│
├── schema/sql/schema.sql       # PostgreSQL schema (10 tables)
├── proto/internal.proto        # gRPC service definitions
├── openapi/metadata_openapi.yaml  # REST API spec (OpenAPI 3.0)
│
├── tests/
│   ├── conftest.py             # Fixtures: DB pool, mock storage, HTTP clients
│   ├── test_chunker.py         # Unit tests — chunking logic (no DB)
│   ├── test_deduplication.py   # Integration — dedup, ref_count, two-user scenario
│   ├── test_sync_conflict.py   # Concurrency — race condition regression tests
│   ├── test_resumability.py    # Interrupted upload / retry behaviour
│   └── test_sync_stress.py     # 5–10 device concurrent sync stress tests
│
├── pytest.ini
└── Makefile
```

---

## Running the Services

### Prerequisites
```bash
# PostgreSQL + Redis must be running
createdb dropboxsim

# Generate gRPC Python stubs (from project root)
python -m grpc_tools.protoc \
    -I proto \
    --python_out=metadata_service/app \
    --grpc_python_out=metadata_service/app \
    proto/internal.proto
```

### Block Server (port 8001)
```bash
pip install -r block_server/requirements.txt
uvicorn block_server.app.main:app --host 0.0.0.0 --port 8001
```

### Metadata Service (port 8002 / gRPC 50052)
```bash
pip install -r metadata_service/requirements.txt
uvicorn metadata_service.app.main:app --host 0.0.0.0 --port 8002
python -m metadata_service.app.grpc_server
```

### Environment Variables
| Variable | Default | Service |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:password@localhost:5432/dropboxsim` | Both |
| `REDIS_URL` | `redis://localhost:6379/0` | Metadata |
| `BLOCK_STORAGE_PATH` | `./data/blocks` | Block Server |
| `METADATA_GRPC_PORT` | `50052` | Metadata |

---

## Running the Tests

```bash
createdb dropboxsim_test
pip install -r tests/requirements.txt

# Full suite
TEST_DATABASE_URL="postgresql://<user>@localhost:5432/dropboxsim_test" make test

# With coverage report
make coverage

# Single module
make test-chunker    # no DB needed
make test-dedup
make test-conflict
make test-resume
make test-stress
```

### Test Results (27 tests, ~1.2 s)
```
tests/test_chunker.py          10 passed   pure unit tests
tests/test_deduplication.py     5 passed   dedup + ref_count + two-user scenario
tests/test_resumability.py      4 passed   interrupted upload / idempotent retry
tests/test_sync_conflict.py     4 passed   concurrent writes, no data loss, unique versions
tests/test_sync_stress.py       4 passed   5–10 device concurrent sync
```

---

## Local Demo

### 1. Prerequisites
```bash
brew services start postgresql@15
brew services start redis

createdb dropboxsim
/opt/homebrew/Cellar/postgresql@15/15.15_1/bin/psql -d dropboxsim -f schema/sql/schema.sql

pip3 install -r block_server/requirements.txt
pip3 install -r metadata_service/requirements.txt
pip3 install aiohttp
```

### 2. Generate gRPC stubs
```bash
python3 -m grpc_tools.protoc \
    -I proto \
    --python_out=metadata_service/app \
    --grpc_python_out=metadata_service/app \
    proto/internal.proto
```

### 3. Start both services (two terminals)
```bash
# Terminal 1 — Block Server
DATABASE_URL="postgresql://<your_user>@localhost:5432/dropboxsim" \
uvicorn block_server.app.main:app --port 8001 --reload

# Terminal 2 — Metadata Service
DATABASE_URL="postgresql://<your_user>@localhost:5432/dropboxsim" \
REDIS_URL="redis://localhost:6379/0" \
uvicorn metadata_service.app.main:app --port 8002 --reload
```

### 4. Upload a file (chunked)
Save as `demo_upload.py` and run with `python3 demo_upload.py`:

```python
import asyncio, asyncpg, aiohttp
from client_utils.chunker import chunk_and_upload

DB = "postgresql://<your_user>@localhost:5432/dropboxsim"

async def main():
    # Upload blocks to Block Server
    results = await chunk_and_upload("README.md", "http://localhost:8001")
    print("Blocks:", results)

    # Seed a user + folder directly in DB
    pool = await asyncpg.create_pool(DB)
    user_id   = await pool.fetchval("INSERT INTO users(email) VALUES('demo@x.com') RETURNING id::text")
    folder_id = await pool.fetchval(
        f"INSERT INTO folders(owner_id, name) VALUES('{user_id}'::uuid, 'root') RETURNING id::text")

    # Commit a file version via Metadata Service
    async with aiohttp.ClientSession() as s:
        r = await s.post("http://localhost:8002/files",
            json={"user_id": user_id, "folder_id": folder_id, "name": "README.md"})
        file_id = (await r.json())["file_id"]

        hashes = [b["hash"] for b in results]
        await s.post(f"http://localhost:8002/files/{file_id}/versions",
            json={"user_id": user_id, "block_hashes": hashes})
        print("Version committed. File ID:", file_id)

asyncio.run(main())
```

### 5. Verify via API

```bash
# Check a block exists (deduplication proof)
curl http://localhost:8001/blocks/has/<hash_from_output>

# View file version history
curl http://localhost:8002/files/<file_id>/history

# Interactive API docs
open http://localhost:8001/docs   # Block Server
open http://localhost:8002/docs   # Metadata Service
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| **Content-addressed blocks** | Same bytes anywhere = 1 copy stored. Enables deduplication and resumable uploads for free. |
| **Immutable versions** | Files are never overwritten; every change creates a new `file_version`. Full history is always available. |
| **Set-difference delta sync** | `GetMissingBlocks` = current version blocks NOT IN device's last-known version blocks. Minimal SQL, minimal data transfer. |
| **`SELECT FOR UPDATE` on version commit** | Serializes concurrent writes to the same file, guaranteeing unique `version_number` values under any load. |
| **Redis pub/sub → WebSocket** | Decouples commit from notification. Multiple server instances can all push to their own connected clients. |
| **Sharded storage paths** (`ab/abcdef…`) | Keeps filesystem directories small; avoids inode limits at scale. |
| **`ref_count` on blocks** | Safe GC — a block is deletable only when its ref_count reaches 0. |

---

## Conversation Log (prompts)

1. User: initial implementation request — Block Server, Metadata Service, Sync Logic (Python/FastAPI, PostgreSQL, Redis, gRPC, WebSockets).

2. User: "start with DB and API"

3. Assistant: created `schema/sql/schema.sql`, `proto/internal.proto`, `openapi/metadata_openapi.yaml`.

4. User: "use one readme. explain this project and log this conversation prompts"

5. User: full implementation prompt (same scope as prompt 1, pasted into Claude Code).

6. Assistant: explored existing codebase, implemented remaining components:
   - `client_utils/chunker.py` — async file chunker with concurrent uploads
   - `metadata_service/app/db.py` — all PostgreSQL operations
   - `metadata_service/app/notifier.py` — Redis pub/sub → WebSocket fan-out
   - `metadata_service/app/main.py` — FastAPI HTTP + WebSocket server
   - `metadata_service/app/grpc_server.py` — gRPC server (three services)

7. User: "where the file itself is saved?"

8. Assistant: explained that block binary data goes to local filesystem (`data/blocks/<shard>/<hash>`); only metadata goes to PostgreSQL. `s3_key` stores the local path as a stand-in for a real S3 key.

9. User: "when I uploaded a file, is it saved in S3 or the DB?"

10. Assistant: clarified — neither real S3 nor the DB holds binary data. Swapping to real S3 only requires replacing `save_block()` / `read_block()` in `storage.py`.

11. User: "log this conversation prompts into the README.md"

12. User: test suite prompt — Chunker unit tests, Deduplication integration, Sync conflict race condition, Interrupted upload resumability.

13. Assistant: implemented 27-test suite across 5 files. Fixed a race condition in `commit_file_version` (`SELECT FOR UPDATE`). Fixed pytest-asyncio 1.x event-loop scope mismatch (`asyncio_default_test_loop_scope = session`).

14. User: "run tests" — all 27 passed in 1.29 s.

15. User: "remove __init__ files if empty" — removed 4 empty `__init__.py` files; tests still green.

16. User: "explain this project architecture" — full architectural walkthrough provided.

-- end of log --
