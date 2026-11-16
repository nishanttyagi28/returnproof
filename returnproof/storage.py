"""Pluggable evidence file storage.

Local disk is the default. The :class:`Storage` protocol is intentionally small
so an S3 implementation (or any object store) can be dropped in without
touching the evidence service or routes.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol

from returnproof.config import Settings


class StorageError(RuntimeError):
    """Raised when a storage backend cannot complete an operation."""


class Storage(Protocol):
    backend_name: str

    def save(self, key: str, data: bytes) -> int: ...

    def read(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def local_path(self, key: str) -> Path | None: ...


class LocalDiskStorage:
    """Filesystem storage rooted under a configured directory."""

    backend_name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError("Path escapes the evidence root")
        return candidate

    def save(self, key: str, data: bytes) -> int:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return len(data)

    def read(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def local_path(self, key: str) -> Path | None:
        return self._resolve(key)


def build_storage(settings: Settings) -> Storage:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalDiskStorage(settings.upload_dir)
    raise StorageError(f"Unsupported storage backend: {settings.storage_backend}")


def make_storage_key(org_id: int, order_id: int, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix and (len(suffix) > 10 or not suffix[1:].isalnum()):
        suffix = ""
    return f"org_{org_id}/order_{order_id}/{uuid.uuid4().hex}{suffix}"
