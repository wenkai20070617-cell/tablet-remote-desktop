"""Resumable file transfer between the browser and this host.

Transfers are chunked over plain HTTPS requests so a dropped connection only
loses the chunk in flight, not the whole file.  A partial upload keeps its
``.part`` file and its recorded offset, and the same ``transferId`` resumes it.
Every chunk is acknowledged with the authoritative offset, so a client that
lost the response can ask and continue instead of restarting.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

CHUNK_READ = 1 << 20


class FileTransferError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class UnsafePath(FileTransferError):
    pass


def safe_name(name: str) -> str:
    """Reduce a client-supplied name to one path component."""
    if not isinstance(name, str):
        raise UnsafePath("invalid file name")
    cleaned = name.replace("\\", "/").split("/")[-1].strip().strip(".")
    cleaned = "".join(ch for ch in cleaned if ch >= " " and ch not in '<>:"|?*')
    if not cleaned or cleaned in {".", ".."}:
        raise UnsafePath("invalid file name")
    if len(cleaned) > 180:
        stem, dot, suffix = cleaned.rpartition(".")
        cleaned = (stem[:160] + dot + suffix[:16]) if dot else cleaned[:160]
    return cleaned


def safe_relative(name: str) -> Path:
    """Validate a client-supplied relative path and return its components."""
    if not isinstance(name, str) or not name.strip():
        raise UnsafePath("invalid file name")
    text = name.replace("\\", "/").strip()
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise UnsafePath("absolute paths are not allowed")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise UnsafePath("path traversal is not allowed")
    if len(parts) > 12:
        raise UnsafePath("path is nested too deeply")
    return Path(*[safe_name(part) for part in parts])


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


def directory_listing(root: Path, relative: str, limit: int = 500) -> dict[str, Any]:
    target = (root / safe_relative(relative)).resolve() if relative else root.resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise UnsafePath("path is outside the shared root")
    if not target.is_dir():
        raise FileTransferError("directory not found", status=404)
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError as exc:
        raise FileTransferError(f"directory could not be read: {exc}", status=503) from exc
    for child in children[:limit]:
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "directory": child.is_dir(),
                "size": int(stat.st_size) if child.is_file() else 0,
                "modified": int(stat.st_mtime),
            }
        )
    return {
        "path": "" if target == root_resolved else str(target.relative_to(root_resolved)).replace("\\", "/"),
        "entries": entries,
        "truncated": len(children) > limit,
    }


class Transfer:
    __slots__ = (
        "transfer_id", "name", "directory", "total", "received", "digest",
        "path", "part_path", "created_at", "updated_at", "cancelled", "kind",
        "verify_hash", "file", "resume_hashes",
    )

    def __init__(
        self,
        transfer_id: str,
        name: str,
        directory: Path,
        total: int,
        path: Path,
        part_path: Path,
        kind: str = "upload",
    ) -> None:
        self.transfer_id = transfer_id
        self.name = name
        self.directory = directory
        self.total = int(total)
        self.received = 0
        self.digest = hashlib.sha256()
        self.path = path
        self.part_path = part_path
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.cancelled = False
        self.kind = kind
        self.verify_hash = ""
        self.file = None
        self.resume_hashes: list[str] = []

    def snapshot(self) -> dict[str, Any]:
        return {
            "transferId": self.transfer_id,
            "name": self.name,
            "total": self.total,
            "received": self.received,
            "progress": round(self.received / self.total, 4) if self.total else 0.0,
            "complete": self.received >= self.total and self.total > 0,
            "cancelled": self.cancelled,
            "path": str(self.path),
            "kind": self.kind,
        }


class TransferManager:
    """Owns in-flight uploads and the shared root used for browsing."""

    def __init__(
        self,
        upload_root: Path,
        download_root: Path,
        max_size: int,
        reserve_free_bytes: int,
        max_active: int = 16,
        max_age_seconds: float = 3600.0,
    ) -> None:
        self.upload_root = Path(upload_root)
        self.download_root = Path(download_root)
        self.max_size = int(max_size)
        self.reserve_free_bytes = int(reserve_free_bytes)
        self.max_active = max(1, int(max_active))
        self.max_age_seconds = max(60.0, float(max_age_seconds))
        self._transfers: dict[str, Transfer] = {}
        self._lock = asyncio.Lock()
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.download_root.mkdir(parents=True, exist_ok=True)

    # -- helpers -------------------------------------------------------
    def _prune(self) -> None:
        now = time.time()
        for transfer_id, transfer in list(self._transfers.items()):
            if now - transfer.updated_at > self.max_age_seconds:
                self._transfers.pop(transfer_id, None)
                self._release(transfer)

    def _release(self, transfer: Transfer) -> None:
        if transfer.file is not None:
            try:
                transfer.file.close()
            except Exception:
                pass
            transfer.file = None

    def _check_space(self, directory: Path, needed: int) -> None:
        try:
            usage = shutil.disk_usage(directory)
        except OSError:
            return
        if usage.free - self.reserve_free_bytes < needed:
            raise FileTransferError("not enough free disk space on the host", status=507)

    @staticmethod
    def transfer_id_for(name: str, total: int) -> str:
        digest = hashlib.sha256(f"{name}:{total}".encode("utf-8")).digest()[:12]
        return digest.hex()

    @staticmethod
    def _hash_partial(path: Path, size: int) -> tuple[Any, list[str]]:
        digest = hashlib.sha256()
        blocks: list[str] = []
        with open(path, "rb") as existing_file:
            remaining = int(size)
            while remaining > 0:
                block = existing_file.read(min(CHUNK_READ, remaining))
                if not block:
                    break
                digest.update(block)
                blocks.append(hashlib.sha256(block).hexdigest())
                remaining -= len(block)
        return digest, blocks

    # -- upload --------------------------------------------------------
    async def begin(
        self,
        name: str,
        total: int,
        relative: str = "",
        overwrite: bool = False,
    ) -> Transfer:
        async with self._lock:
            self._prune()
            if total < 0 or total > self.max_size:
                raise FileTransferError("file is larger than the configured limit", status=413)
            relative_path = safe_relative(relative) if relative else Path(safe_name(name))
            if relative_path.parent == Path("."):
                directory = self.upload_root
                final_name = relative_path.name
            else:
                directory = self.upload_root / relative_path.parent
                final_name = relative_path.name
            directory.mkdir(parents=True, exist_ok=True)
            relative_id = str(relative_path).replace("\\", "/")
            transfer_id = self.transfer_id_for(relative_id, int(total))
            existing = self._transfers.get(transfer_id)
            if existing is not None and not existing.cancelled:
                if existing.received < existing.total:
                    if existing.file is not None:
                        existing.file.flush()
                    existing.digest, existing.resume_hashes = await asyncio.to_thread(
                        self._hash_partial, existing.part_path, existing.received
                    )
                    return existing
                self._transfers.pop(transfer_id, None)
                self._release(existing)
            part_path = directory / f".{final_name}.{transfer_id}.part"
            if part_path.exists() and part_path.stat().st_size > int(total):
                part_path.unlink(missing_ok=True)
            if overwrite:
                target = directory / final_name
            else:
                target = unique_path(directory, final_name)
            transfer = Transfer(transfer_id, final_name, directory, total, target, part_path)
            self._check_space(directory, int(total))
            if len(self._transfers) >= self.max_active:
                oldest = min(self._transfers.values(), key=lambda item: item.updated_at)
                self._transfers.pop(oldest.transfer_id, None)
                self._release(oldest)
            offset = part_path.stat().st_size if part_path.exists() else 0
            transfer.file = open(part_path, "r+b" if offset else "wb")
            if offset:
                transfer.digest, transfer.resume_hashes = await asyncio.to_thread(
                    self._hash_partial, part_path, offset
                )
                transfer.received = offset
                transfer.file.seek(offset)
            self._transfers[transfer_id] = transfer
            return transfer

    async def get(self, transfer_id: str) -> Transfer:
        async with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                raise FileTransferError("unknown transfer", status=404)
            return transfer

    async def status(self, name: str, total: int, relative: str = "") -> dict[str, Any]:
        transfer_id = self.transfer_id_for(
            str(safe_relative(relative)).replace("\\", "/") if relative else safe_name(name),
            int(total),
        )
        async with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return {"transferId": transfer_id, "received": 0, "known": False}
            return {**transfer.snapshot(), "known": True}

    async def write_chunk(
        self,
        transfer_id: str,
        offset: int,
        chunk: bytes,
        chunk_sha256: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                raise FileTransferError("unknown transfer", status=404)
            if transfer.cancelled:
                raise FileTransferError("transfer was cancelled", status=409)
            if transfer.received >= transfer.total and transfer.total > 0:
                raise FileTransferError("transfer is already complete", status=409)
            if offset != transfer.received:
                # Out-of-order or repeated chunk: tell the client where to resume.
                return {**transfer.snapshot(), "ok": False, "expectedOffset": transfer.received}
            if transfer.received + len(chunk) > transfer.total:
                raise FileTransferError("chunk exceeds the declared size", status=413)
            if chunk_sha256:
                actual_chunk_hash = hashlib.sha256(chunk).hexdigest()
                if not hmac.compare_digest(chunk_sha256.lower(), actual_chunk_hash):
                    raise FileTransferError("chunk checksum mismatch", status=422)
            transfer.file.write(chunk)
            transfer.digest.update(chunk)
            transfer.received += len(chunk)
            transfer.updated_at = time.time()
            if transfer.received % (8 * CHUNK_READ) < len(chunk):
                transfer.file.flush()
            return {**transfer.snapshot(), "ok": True, "expectedOffset": transfer.received}

    async def finish(self, transfer_id: str, sha256: str = "") -> dict[str, Any]:
        async with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                raise FileTransferError("unknown transfer", status=404)
            if transfer.cancelled:
                raise FileTransferError("transfer was cancelled", status=409)
            if transfer.total and transfer.received != transfer.total:
                raise FileTransferError(
                    f"transfer is incomplete: {transfer.received} of {transfer.total} bytes",
                    status=409,
                )
            return await self._complete(transfer, sha256)

    async def _complete(self, transfer: Transfer, sha256: str) -> dict[str, Any]:
        transfer.file.flush()
        os.fsync(transfer.file.fileno())
        self._release(transfer)
        actual = transfer.digest.hexdigest()
        if sha256 and sha256.lower() != actual:
            transfer.part_path.unlink(missing_ok=True)
            self._transfers.pop(transfer.transfer_id, None)
            raise FileTransferError("checksum mismatch; the upload was discarded", status=422)
        transfer.part_path.replace(transfer.path)
        self._transfers.pop(transfer.transfer_id, None)
        return {
            "ok": True,
            "name": transfer.name,
            "path": str(transfer.path),
            "size": transfer.total,
            "sha256": actual,
        }

    async def cancel(self, transfer_id: str, remove_partial: bool = False) -> dict[str, Any]:
        async with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return {"ok": True, "known": False}
            transfer.cancelled = True
            self._release(transfer)
            self._transfers.pop(transfer_id, None)
            if remove_partial:
                transfer.part_path.unlink(missing_ok=True)
            return {"ok": True, "known": True, "keptPartial": not remove_partial}

    async def active(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [transfer.snapshot() for transfer in self._transfers.values()]

    async def close(self) -> None:
        """Close every open partial file without deleting resumable data."""
        async with self._lock:
            transfers = list(self._transfers.values())
            self._transfers.clear()
            for transfer in transfers:
                self._release(transfer)

    # -- download ------------------------------------------------------
    def resolve_download(self, relative: str) -> Path:
        target = (self.download_root / safe_relative(relative)).resolve()
        root = self.download_root.resolve()
        if target != root and root not in target.parents:
            raise UnsafePath("path is outside the shared root")
        if not target.is_file():
            raise FileTransferError("file not found", status=404)
        return target

    def delete_file(self, relative: str, root_kind: str = "upload") -> dict[str, Any]:
        """Delete one regular file below an explicitly selected shared root."""
        if root_kind not in {"upload", "download"}:
            raise FileTransferError("unknown shared root", status=400)
        root = self.download_root if root_kind == "download" else self.upload_root
        root_resolved = root.resolve()
        target = (root_resolved / safe_relative(relative)).resolve()
        if target == root_resolved or root_resolved not in target.parents:
            raise UnsafePath("path is outside the shared root")
        if not target.exists():
            raise FileTransferError("file not found", status=404)
        if not target.is_file():
            raise FileTransferError("only regular files can be deleted", status=409)
        try:
            target.unlink()
        except OSError as exc:
            raise FileTransferError(f"file could not be deleted: {exc}", status=503) from exc
        return {"ok": True, "path": str(target.relative_to(root_resolved)).replace("\\", "/")}
