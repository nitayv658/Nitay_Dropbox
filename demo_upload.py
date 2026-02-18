import asyncio
import asyncpg
import aiohttp
from client_utils.chunker import chunk_and_upload

DB = "postgresql://nitay658@localhost:5432/dropboxsim"

async def main():
    print("=== Step 1: Chunk & upload blocks ===")
    results = await chunk_and_upload("README.md", "http://localhost:8001")
    for r in results:
        status = "NEW   " if r["stored"] else "DEDUP "
        print(f"  [{status}] block {r['block_index']}  hash={r['hash'][:16]}...  key={r['s3_key']}")

    print(f"\n=== Step 2: Seed user + folder in DB ===")
    pool = await asyncpg.create_pool(DB)
    user_id = await pool.fetchval(
        "INSERT INTO users(email, display_name) VALUES('demo@example.com', 'Demo User') "
        "ON CONFLICT (email) DO UPDATE SET display_name='Demo User' RETURNING id::text"
    )
    folder_id = await pool.fetchval(
        f"INSERT INTO folders(owner_id, name) VALUES('{user_id}'::uuid, 'root') RETURNING id::text"
    )
    print(f"  user_id   = {user_id}")
    print(f"  folder_id = {folder_id}")

    print(f"\n=== Step 3: Create file + commit version ===")
    async with aiohttp.ClientSession() as s:
        r = await s.post("http://localhost:8002/files",
            json={"user_id": user_id, "folder_id": folder_id, "name": "README.md"})
        file_id = (await r.json())["file_id"]
        print(f"  file_id   = {file_id}")

        hashes = [b["hash"] for b in results]
        vr = await s.post(f"http://localhost:8002/files/{file_id}/versions",
            json={"user_id": user_id, "block_hashes": hashes})
        ver = await vr.json()
        print(f"  version   = {ver}")

    print(f"\n=== Step 4: Verify deduplication — upload same file again ===")
    results2 = await chunk_and_upload("README.md", "http://localhost:8001")
    new_count  = sum(1 for r in results2 if r["stored"])
    dedup_count = sum(1 for r in results2 if not r["stored"])
    print(f"  stored={new_count}  deduped={dedup_count}  (all {dedup_count} blocks skipped)")

    print(f"\n=== Step 5: Fetch version history ===")
    async with aiohttp.ClientSession() as s:
        hr = await s.get(f"http://localhost:8002/files/{file_id}/history")
        history = (await hr.json())["versions"]
        for v in history:
            print(f"  version {v['version_number']}  id={v['id'][:16]}...  size={v['size']} bytes")

    print("\nDone.")
    await pool.close()

asyncio.run(main())
