"""
Block storage backends.

Implements a StorageBackend ABC with two concrete implementations:
  - LocalStorageBackend: filesystem-based (current default, mock S3)
  - S3StorageBackend: real AWS S3 via boto3 (enable with STORAGE_BACKEND=s3)

The module-level functions (save_block, read_block, exists_block, delete_block)
delegate to the active backend so callers and test patches remain unchanged.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Tuple

from .config import settings


# ─── Abstract interface ────────────────────────────────────────────────────────


class StorageBackend(ABC):
    @abstractmethod
    def save_block(self, hash_hex: str, data: bytes) -> str:
        """Persist block data. Returns the storage key (path or S3 URI)."""

    @abstractmethod
    def read_block(self, hash_hex: str) -> Tuple[bytes, str]:
        """Read block data. Returns (data, storage_key)."""

    @abstractmethod
    def exists_block(self, hash_hex: str) -> Tuple[bool, str]:
        """Check existence. Returns (exists, storage_key)."""

    @abstractmethod
    def delete_block(self, hash_hex: str) -> None:
        """Delete a block from storage (called by GC)."""


# ─── Local filesystem backend ─────────────────────────────────────────────────


class LocalStorageBackend(StorageBackend):
    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir).absolute()
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, hash_hex: str) -> Path:
        # Shard by first 2 hex chars to avoid filesystem inode limits
        p = self.base / hash_hex[:2] / hash_hex
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def save_block(self, hash_hex: str, data: bytes) -> str:
        p = self._path(hash_hex)
        p.write_bytes(data)
        return str(p)

    def read_block(self, hash_hex: str) -> Tuple[bytes, str]:
        p = self._path(hash_hex)
        if not p.exists():
            raise FileNotFoundError(f"Block not found in storage: {hash_hex}")
        return p.read_bytes(), str(p)

    def exists_block(self, hash_hex: str) -> Tuple[bool, str]:
        p = self._path(hash_hex)
        return p.exists(), str(p)

    def delete_block(self, hash_hex: str) -> None:
        p = self._path(hash_hex)
        if p.exists():
            p.unlink()


# ─── S3 backend (stub — set STORAGE_BACKEND=s3 to enable) ────────────────────


class S3StorageBackend(StorageBackend):
    """
    AWS S3 storage backend.
    Requires: boto3, AWS_BUCKET, AWS_REGION (+ credentials via env or IAM role).
    """

    def __init__(self) -> None:
        import boto3  # lazily imported so missing boto3 doesn't break local dev

        self.client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
        )
        self.bucket = settings.aws_bucket

    def _key(self, hash_hex: str) -> str:
        return f"blocks/{hash_hex[:2]}/{hash_hex}"

    def save_block(self, hash_hex: str, data: bytes) -> str:
        key = self._key(hash_hex)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return f"s3://{self.bucket}/{key}"

    def read_block(self, hash_hex: str) -> Tuple[bytes, str]:
        key = self._key(hash_hex)
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read(), f"s3://{self.bucket}/{key}"

    def exists_block(self, hash_hex: str) -> Tuple[bool, str]:
        import botocore.exceptions

        key = self._key(hash_hex)
        uri = f"s3://{self.bucket}/{key}"
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True, uri
        except botocore.exceptions.ClientError:
            return False, ""

    def delete_block(self, hash_hex: str) -> None:
        key = self._key(hash_hex)
        self.client.delete_object(Bucket=self.bucket, Key=key)


# ─── Factory + module-level API ───────────────────────────────────────────────


def _make_backend() -> StorageBackend:
    if settings.storage_backend == "s3":
        return S3StorageBackend()
    return LocalStorageBackend(settings.block_storage_path)


_backend: StorageBackend = _make_backend()


# Module-level functions keep backward compatibility with existing callers
# and test patches (tests patch these names directly).

def save_block(hash_hex: str, data: bytes) -> str:
    return _backend.save_block(hash_hex, data)


def read_block(hash_hex: str) -> Tuple[bytes, str]:
    return _backend.read_block(hash_hex)


def exists_block(hash_hex: str) -> Tuple[bool, str]:
    return _backend.exists_block(hash_hex)


def delete_block(hash_hex: str) -> None:
    return _backend.delete_block(hash_hex)
