# Block Server (Python / FastAPI)

This is a lightweight Block Server scaffold that:
- Accepts block uploads (up to 4MB)
- Deduplicates by SHA-256 hash against the `blocks` table
- Stores block bytes in a local filesystem path (mocked S3)

Requirements

Install dependencies (preferably in a venv):

```bash
cd block_server
python -m pip install -r requirements.txt
```

Database

Ensure PostgreSQL is available and run the SQL in `schema/sql/schema.sql` to create the `blocks` table and others. Set `DATABASE_URL` environment variable, for example:

```bash
export DATABASE_URL=postgresql://postgres:password@localhost:5432/dropboxsim
```

Run the server (development):

```bash
uvicorn block_server.app.main:app --reload --host 0.0.0.0 --port 8001
```

Upload block example (curl):

```bash
curl -X POST "http://localhost:8001/blocks/upload" -F "hash=..." -F "size=123" -F "file=@./block.bin"
```
