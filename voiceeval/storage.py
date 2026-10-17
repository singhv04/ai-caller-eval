"""Blob storage abstraction.

Everything the platform persists besides DB rows (audio, sidecar transcripts,
analysis caches) goes through a `Storage` backend addressed by string keys.
`LocalStorage` is the default; an object-store backend (S3/GCS) only needs to
implement the same five methods so API servers and workers can run on
different machines.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol

from .config import get_settings


class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def local_path(self, key: str) -> Path: ...
    def delete_prefix(self, prefix: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"Invalid storage key: {key}")
        return p

    def put_bytes(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def local_path(self, key: str) -> Path:
        """A filesystem path for the blob (remote backends download to a temp cache)."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def delete_prefix(self, prefix: str) -> None:
        p = self._path(prefix)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    # convenience helpers
    def put_json(self, key: str, obj: Any) -> None:
        self.put_bytes(key, json.dumps(obj, default=str).encode())

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_bytes(key))


_storage: LocalStorage | None = None


def get_storage() -> LocalStorage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(get_settings().data_dir / "blobs")
    return _storage
