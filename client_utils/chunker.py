"""
File Chunking Utility

Breaks a file into fixed-size 4 MB blocks, computes SHA-256 hashes for
deduplication, and uploads blocks concurrently to the Block Server.

Production features:
  - Retry with exponential backoff (up to 3 attempts per block)
  - Per-request timeout (60 s)
  - Service-to-service API key header (X-Service-Key)
  - Partial failure handling: raises if any chunk fails after all retries

Usage:
    import asyncio
    from client_utils.chunker import chunk_and_upload

    results = asyncio.run(chunk_and_upload(
        "bigfile.bin",
        "http://localhost:8001",
        api_key="my-service-key",   # optional
    ))
    # [{"block_index": 0, "hash": "abc...", "stored": True, "s3_key": "..."}, ...]
"""

import asyncio
import hashlib
from typing import Iterator, List, Optional, Tuple

import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB


def chunk_file(
    filepath: str, chunk_size: int = CHUNK_SIZE
) -> Iterator[Tuple[int, str, bytes]]:
    """
    Yield (block_index, sha256_hex, data) for each fixed-size chunk of the file.
    The last chunk may be smaller than chunk_size.
    """
    with open(filepath, "rb") as f:
        index = 0
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            hash_hex = hashlib.sha256(data).hexdigest()
            yield (index, hash_hex, data)
            index += 1


def _make_upload_func(block_server_url: str, api_key: Optional[str]):
    """Return a retry-wrapped async upload function closed over URL and API key."""

    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, aiohttp.ServerTimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _upload(
        session: aiohttp.ClientSession,
        index: int,
        hash_hex: str,
        data: bytes,
    ) -> dict:
        url = f"{block_server_url.rstrip('/')}/blocks/upload"
        headers = {"X-Service-Key": api_key} if api_key else {}
        form = aiohttp.FormData()
        form.add_field(
            "file",
            data,
            filename=hash_hex,
            content_type="application/octet-stream",
        )
        timeout = aiohttp.ClientTimeout(total=60)
        async with session.post(
            url,
            params={"hash": hash_hex, "size": len(data)},
            data=form,
            headers=headers,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
        return {"block_index": index, "hash": hash_hex, **result}

    return _upload


# Keep the original upload_chunk signature for backward compatibility
async def upload_chunk(
    session: aiohttp.ClientSession,
    block_server_url: str,
    index: int,
    hash_hex: str,
    data: bytes,
    api_key: Optional[str] = None,
) -> dict:
    """Upload a single block to the Block Server via multipart POST (with retry)."""
    _upload = _make_upload_func(block_server_url, api_key)
    return await _upload(session, index, hash_hex, data)


async def chunk_and_upload(
    filepath: str,
    block_server_url: str,
    chunk_size: int = CHUNK_SIZE,
    concurrency: int = 8,
    api_key: Optional[str] = None,
) -> List[dict]:
    """
    Chunk a file and upload all blocks concurrently to the Block Server.

    Args:
        filepath:         Path to the file to upload.
        block_server_url: Base URL of the Block Server.
        chunk_size:       Block size in bytes (default 4 MB).
        concurrency:      Maximum concurrent in-flight uploads (1–32).
        api_key:          Optional X-Service-Key header value.

    Returns:
        List of block results sorted by block_index:
        [{"block_index": 0, "hash": "...", "stored": True/False, "s3_key": "..."}, ...]

    Raises:
        RuntimeError: If any block upload fails after all retries.
    """
    concurrency = max(1, min(concurrency, 32))
    chunks = list(chunk_file(filepath, chunk_size))
    sem = asyncio.Semaphore(concurrency)
    _upload = _make_upload_func(block_server_url, api_key)

    async def bounded_upload(
        session: aiohttp.ClientSession, index: int, hash_hex: str, data: bytes
    ) -> dict:
        async with sem:
            return await _upload(session, index, hash_hex, data)

    async with aiohttp.ClientSession() as session:
        tasks = [
            bounded_upload(session, index, hash_hex, data)
            for index, hash_hex, data in chunks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError(
            f"{len(failures)} block upload(s) failed after retries: "
            + ", ".join(str(f) for f in failures)
        )

    return sorted(
        [r for r in results if not isinstance(r, Exception)],
        key=lambda r: r["block_index"],
    )
