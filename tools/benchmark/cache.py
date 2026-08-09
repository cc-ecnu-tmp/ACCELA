from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .errors import ExecutionError
from .util import atomic_write_json, read_json, sha256_file


@dataclass(frozen=True)
class CacheEntry:
    artifact: Path
    phase: dict[str, object]
    samples: tuple[dict[str, object], ...]
    statistics: dict[str, object] | None
    hit: bool


@dataclass(frozen=True)
class CompileBuild:
    phase: dict[str, object]
    samples: tuple[dict[str, object], ...]
    statistics: dict[str, object] | None


class CompileCache:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root.resolve() / "cache" / "compile"
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _thread_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def _file_lock(self, key: str, timeout_seconds: float = 300) -> Iterator[None]:
        lock_path = self.root / f"{key}.lock"
        deadline = time.monotonic() + timeout_seconds
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > 3600
                except FileNotFoundError:
                    continue
                if stale:
                    lock_path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise ExecutionError("timed out waiting for compile cache lock")
                time.sleep(0.05)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def _load(self, key: str, suffix: str) -> CacheEntry | None:
        directory = self.root / key
        artifact = directory / f"artifact{suffix}"
        metadata_path = directory / "metadata.json"
        if not directory.exists():
            return None
        if not directory.is_dir() or not artifact.is_file() or not metadata_path.is_file():
            raise ExecutionError(f"compile cache entry {key} is incomplete")
        try:
            metadata = read_json(metadata_path)
            if (
                not isinstance(metadata, dict)
                or metadata.get("version") != 2
                or metadata.get("key") != key
            ):
                raise ExecutionError(f"compile cache entry {key} has invalid metadata")
            if metadata.get("artifact_sha256") != sha256_file(artifact):
                raise ExecutionError(f"compile cache entry {key} failed artifact integrity verification")
            remarks = directory / "remarks.jsonl"
            remarks_sha256 = metadata.get("remarks_sha256")
            if remarks_sha256 is not None and (
                not remarks.is_file() or sha256_file(remarks) != remarks_sha256
            ):
                raise ExecutionError(f"compile cache entry {key} failed remarks integrity verification")
            phase = metadata["phase"]
            if not isinstance(phase, dict):
                raise ExecutionError(f"compile cache entry {key} has invalid phase metadata")
            samples = metadata["samples"]
            statistics = metadata["statistics"]
            if not isinstance(samples, list) or not all(isinstance(item, dict) for item in samples):
                raise ExecutionError(f"compile cache entry {key} has invalid sample metadata")
            for index, sample in enumerate(samples):
                repetition = directory / f"repetition-{index:04d}"
                sample_artifact = repetition / f"artifact{suffix}"
                if (
                    not sample_artifact.is_file()
                    or sample.get("artifact_sha256") != sha256_file(sample_artifact)
                    or sample.get("artifact_size_bytes") != sample_artifact.stat().st_size
                ):
                    raise ExecutionError(
                        f"compile cache entry {key} failed cold sample artifact integrity verification"
                    )
                sample_remarks_sha256 = sample.get("remarks_sha256")
                sample_remarks_count = sample.get("remarks_event_count")
                sample_remarks = repetition / "remarks.jsonl"
                if (sample_remarks_sha256 is None) != (sample_remarks_count is None):
                    raise ExecutionError(
                        f"compile cache entry {key} has inconsistent cold sample remarks metadata"
                    )
                if sample_remarks_sha256 is not None and (
                    not sample_remarks.is_file()
                    or sha256_file(sample_remarks) != sample_remarks_sha256
                ):
                    raise ExecutionError(
                        f"compile cache entry {key} failed cold sample remarks integrity verification"
                    )
                if sample_remarks_sha256 is not None:
                    if (
                        not isinstance(sample_remarks_count, int)
                        or isinstance(sample_remarks_count, bool)
                        or sample_remarks_count < 0
                    ):
                        raise ExecutionError(
                            f"compile cache entry {key} has invalid cold sample remarks event count"
                        )
                    with sample_remarks.open("rb") as stream:
                        observed_event_count = sum(bool(line.strip()) for line in stream)
                    if observed_event_count != sample_remarks_count:
                        raise ExecutionError(
                            f"compile cache entry {key} failed cold sample remarks event-count integrity verification"
                        )
            if statistics is not None and not isinstance(statistics, dict):
                raise ExecutionError(f"compile cache entry {key} has invalid statistics metadata")
        except (KeyError, TypeError) as exc:
            raise ExecutionError(f"compile cache entry {key} has invalid metadata") from exc
        cached_phase = dict(phase)
        cached_phase["diagnostic"] = "compile cache hit; duration is the cached observation"
        return CacheEntry(artifact, cached_phase, tuple(samples), statistics, True)

    def get_or_build(
        self,
        key: str,
        suffix: str,
        builder: Callable[[Path, Path], CompileBuild],
        *,
        read_allowed: bool,
    ) -> CacheEntry:
        lock = self._thread_lock(key)
        with lock, self._file_lock(key):
            cached = self._load(key, suffix)
            if cached is not None and read_allowed:
                return cached
            temporary = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=self.root))
            artifact = temporary / f"artifact{suffix}"
            try:
                build = builder(artifact, temporary)
                if build.phase["status"] != "ok":
                    return CacheEntry(artifact, build.phase, build.samples, build.statistics, False)
                if not artifact.is_file():
                    phase = dict(build.phase)
                    phase["status"] = "error"
                    phase["diagnostic"] = "compiler exited successfully without creating {artifact}"
                    return CacheEntry(artifact, phase, build.samples, build.statistics, False)
                metadata = {
                    "version": 2,
                    "key": key,
                    "artifact_sha256": sha256_file(artifact),
                    "phase": build.phase,
                    "samples": list(build.samples),
                    "statistics": build.statistics,
                    "remarks_sha256": (
                        sha256_file(temporary / "remarks.jsonl")
                        if (temporary / "remarks.jsonl").is_file()
                        else None
                    ),
                }
                atomic_write_json(temporary / "metadata.json", metadata)
                destination = self.root / key
                if destination.exists():
                    cached = self._load(key, suffix)
                    assert cached is not None
                    if sha256_file(artifact) != sha256_file(cached.artifact):
                        raise ExecutionError("cold compiler repetitions disagree with the existing cache artifact")
                    return CacheEntry(cached.artifact, build.phase, build.samples, build.statistics, False)
                try:
                    os.replace(temporary, destination)
                except OSError as exc:
                    if destination.exists():
                        cached = self._load(key, suffix)
                        if cached is not None:
                            return cached
                    raise ExecutionError(f"cannot publish compile cache entry: {exc}") from exc
                return CacheEntry(
                    destination / f"artifact{suffix}",
                    build.phase,
                    build.samples,
                    build.statistics,
                    False,
                )
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
