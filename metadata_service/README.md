# Metadata Service

FastAPI + gRPC service covering:
- **Metadata** — directories, files, version history
- **Sync Engine** — device-cursor diffing, missing-block computation, WebSocket push notifications
- **Sharing Module** — scoped shareable links, folder access grants

---

## Prerequisites

- Python 3.10+
- PostgreSQL with the schema applied (`schema/sql/schema.sql`)
- Redis (for WebSocket pub/sub)

---

## Setup

```bash
# 1. Install dependencies
pip install -r metadata_service/requirements.txt

# 2. Generate gRPC Python stubs from the project root
python -m grpc_tools.protoc \
    -I proto \
    --python_out=metadata_service/app \
    --grpc_python_out=metadata_service/app \
    proto/internal.proto

# 3. Apply the database schema (first time only)
psql -U postgres -d dropboxsim -f schema/sql/schema.sql
```

---

## Environment Variables

| Variable              | Default                                               | Description            |
|-----------------------|-------------------------------------------------------|------------------------|
| `DATABASE_URL`        | `postgresql://postgres:password@localhost:5432/dropboxsim` | PostgreSQL DSN    |
| `REDIS_URL`           | `redis://localhost:6379/0`                            | Redis connection URL    |
| `METADATA_GRPC_PORT`  | `50052`                                               | gRPC listen port       |

---

## Running

### HTTP + WebSocket server (port 8002)

```bash
uvicorn metadata_service.app.main:app --reload --host 0.0.0.0 --port 8002
```

### gRPC server (port 50052)

```bash
python -m metadata_service.app.grpc_server
```

---

## REST API Quick Reference

| Method   | Path                            | Description                          |
|----------|---------------------------------|--------------------------------------|
| POST     | `/directories`                  | Create a directory                   |
| DELETE   | `/files/{file_id}?user_id=...`  | Soft-delete a file                   |
| GET      | `/files/{file_id}/history`      | Version history (query: `limit`)     |
| POST     | `/files`                        | Create a file record                 |
| POST     | `/files/{file_id}/versions`     | Commit a new version (block hashes)  |
| POST     | `/folders/{folder_id}/shares`   | Grant folder access to a user        |
| GET      | `/share-links/{token}`          | Validate a shareable link            |

Interactive docs: `http://localhost:8002/docs`

---

## WebSocket Sync Protocol

Connect to `ws://localhost:8002/ws/{device_id}`.

**Client → Server**
```json
{"type": "subscribe",   "file_ids": ["<uuid>", ...]}
{"type": "unsubscribe", "file_ids": ["<uuid>", ...]}
```

**Server → Client**
```json
{"type": "subscribed",   "file_ids": [...]}
{"type": "file_changed", "file_id": "...", "version_id": "...", "timestamp": "2024-..."}
```

---

## Sync Flow (end-to-end)

```
1. Device A uploads blocks      →  POST /blocks/upload        (Block Server :8001)
2. Device A creates file        →  POST /files                (Metadata :8002)
3. Device A commits version     →  POST /files/{id}/versions  (Metadata :8002)
                                     → Redis PUBLISH file:{id}
4. Device B (WebSocket)         ←  {"type":"file_changed",...}
5. Device B computes diff       →  gRPC GetMissingBlocks      (Metadata :50052)
6. Device B fetches blocks      →  GET /blocks/get/{hash}     (Block Server :8001)
```

---

## gRPC Services

| Service          | Methods                                         |
|------------------|-------------------------------------------------|
| MetadataService  | CreateDirectory, DeleteFile, GetFileHistory     |
| SyncService      | GetMissingBlocks                                |
| SharingService   | CreateShareLink                                 |
