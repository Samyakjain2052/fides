"""Where uploaded bytes actually live.

Deliberately NOT the database. A rights request's evidence pack and somebody's
photographed Aadhaar card are multi-megabyte objects, and putting them in
Postgres would bloat every backup and every replica with data that is never
queried — only fetched whole, by primary key. The database keeps the metadata
(who, what, how big, which hash) and the object store keeps the bytes.

Two backends, chosen by `storage_backend`:

  local        a directory on disk. Correct for development and for the test
               suite, and explicitly NOT correct for Container Apps, where the
               filesystem is ephemeral and a revision restart loses everything.
  azure_blob   Azure Blob Storage, which is what production uses.

Everything written here is encrypted before it reaches the backend — see
`file_service`. The backends themselves handle opaque ciphertext and have no
idea what they are holding, which is the point: a misconfigured container ACL
or a stolen storage key yields bytes nobody can read.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

logger = logging.getLogger("app.storage")


class ObjectNotFound(RuntimeError):
    """The key is not in the store.

    Distinct from a decryption failure. "We never had it" and "we have it and
    cannot read it" send an operator to different places.
    """


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalStorage:
    """A directory on disk. Development and tests only.

    Keys are validated rather than trusted. They are generated internally today,
    but a path-traversal hole here would be a file-read primitive over the whole
    container, and "the only caller is us" is not a durable guarantee.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        # Containment check, not string prefix matching: `..` segments and
        # symlinks both resolve away before this comparison.
        if not candidate.is_relative_to(self._root):
            raise ValueError("storage key escapes the storage root")
        return candidate

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name and rename, so a crash mid-write cannot
        # leave a truncated object that later fails authentication and looks
        # like tampering.
        tmp = path.with_suffix(path.suffix + ".partial")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFound(key) from exc

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class AzureBlobStorage:
    """Azure Blob Storage.

    Imported lazily so a development environment without `azure-storage-blob`
    installed still starts. The failure mode we want is "configuring azure_blob
    without the dependency raises at startup", not "every environment must carry
    an Azure SDK to run the test suite".
    """

    def __init__(self, connection_string: str, container: str) -> None:
        try:
            from azure.storage.blob.aio import BlobServiceClient
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "storage_backend=azure_blob requires the azure-storage-blob "
                "package; install it or use storage_backend=local"
            ) from exc
        self._client = BlobServiceClient.from_connection_string(connection_string)
        self._container = container

    async def put(self, key: str, data: bytes) -> None:
        blob = self._client.get_blob_client(self._container, key)
        await blob.upload_blob(data, overwrite=True)

    async def get(self, key: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._client.get_blob_client(self._container, key)
        try:
            stream = await blob.download_blob()
            return await stream.readall()
        except ResourceNotFoundError as exc:
            raise ObjectNotFound(key) from exc

    async def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._client.get_blob_client(self._container, key)
        try:
            await blob.delete_blob()
        except ResourceNotFoundError:
            # Idempotent: deleting an object that is already gone is a success,
            # so a retried purge does not fail the whole run.
            pass


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """The configured backend, built once.

    Not a FastAPI dependency because the retention scheduler needs it too, and
    a job runner has no request to hang a dependency off.
    """
    global _backend
    if _backend is not None:
        return _backend

    settings = get_settings()
    kind = (settings.storage_backend or "local").strip().lower()

    if kind == "azure_blob":
        if not settings.storage_azure_connection_string:
            raise RuntimeError(
                "storage_backend=azure_blob requires "
                "storage_azure_connection_string"
            )
        _backend = AzureBlobStorage(
            settings.storage_azure_connection_string,
            settings.storage_azure_container,
        )
    elif kind == "local":
        _backend = LocalStorage(settings.storage_local_root)
    else:
        raise RuntimeError(f"unknown storage_backend {kind!r}")

    logger.info("object storage backend: %s", kind)
    return _backend


def reset_storage() -> None:
    """Drop the cached backend. For tests that repoint the root."""
    global _backend
    _backend = None
