from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

import portalocker
import psutil

from .errors import ExecutionError
from .util import canonical_json_bytes, sha256_bytes, utc_now


def path_identity(path: Path) -> str:
    """Return a path commitment without persisting the local path itself."""

    canonical = os.path.normcase(os.path.realpath(os.fspath(path.resolve())))
    return sha256_bytes(os.fsencode(canonical))


def output_lease_path(output_path: Path) -> Path:
    identity = path_identity(output_path)
    return output_path.parent / ".accela-benchmark-locks" / f"output-{identity}.lock"


@dataclass
class ExclusiveFileLease:
    """Non-blocking OS-backed lease whose file contains privacy-safe owner metadata.

    The lock file is intentionally permanent. Liveness comes only from the OS
    advisory lock, so a crashed process cannot leave a stale sentinel that blocks
    later work and no process ever unlinks a file another process may have locked.
    """

    path: Path
    role: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        self._lock: portalocker.Lock | None = None
        self._stream: BinaryIO | None = None
        self._owner_token = uuid.uuid4().hex
        self._acquired_at: str | None = None

    def __enter__(self) -> ExclusiveFileLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        lock = portalocker.Lock(
            self.path,
            mode="r+b",
            timeout=0,
            fail_when_locked=True,
            flags=(
                portalocker.LockFlags.EXCLUSIVE
                | portalocker.LockFlags.NON_BLOCKING
            ),
        )
        try:
            stream = lock.acquire()
        except portalocker.exceptions.AlreadyLocked as exc:
            raise ExecutionError(
                f"benchmark {self.role} is already owned by another orchestrator"
            ) from exc
        except portalocker.exceptions.LockException as exc:
            raise ExecutionError(f"cannot acquire benchmark {self.role} lease") from exc
        self._lock = lock
        self._stream = stream
        self._acquired_at = utc_now()
        try:
            self.bind(self.metadata)
        except BaseException:
            self._stream = None
            self._lock = None
            lock.release()
            raise
        return self

    def bind(self, metadata: Mapping[str, Any]) -> None:
        if self._stream is None or self._acquired_at is None:
            raise RuntimeError("cannot bind metadata before acquiring the lease")
        document = {
            "schema_version": "benchmark-run-lock.v1",
            "role": self.role,
            "owner_token": self._owner_token,
            "pid": os.getpid(),
            "process_create_time": psutil.Process().create_time(),
            "acquired_at": self._acquired_at,
            **dict(metadata),
        }
        payload = canonical_json_bytes(document) + b"\n"
        self._stream.seek(0)
        self._stream.truncate(0)
        self._stream.write(payload)
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        lock = self._lock
        self._stream = None
        self._lock = None
        if lock is not None:
            lock.release()
