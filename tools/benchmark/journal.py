from __future__ import annotations

import errno
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .errors import ExecutionError, ValidationError
from .util import (
    canonical_json_bytes,
    describe_os_error,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_json,
)

_EVENT_FILE = re.compile(r"^event-(?P<sequence>[0-9]{4})-(?P<sha256>[0-9a-f]{64})\.json$")
_STAGE_ID = re.compile(r"^(?:compile|link|analyze|run-[0-9]{4})$")
_EVENT_KEYS = {
    "schema_version",
    "sequence",
    "event_type",
    "identity_sha256",
    "previous_event_sha256",
    "payload",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMPILE_RESULT_CORE = frozenset({
    "compile",
    "compile_samples",
    "compile_statistics",
    "cache_hit",
})
_COMPILE_RESULT_SUCCESS = frozenset({
    *_COMPILE_RESULT_CORE,
    "artifact_sha256",
    "remarks_sha256",
    "remarks_event_count",
    "candidate_remark_summary",
})
_PREFIX_TERMINAL_OVERLAY_FIELDS = frozenset(
    {
        "status",
        "cancellation_reason",
        "diagnostic",
        "consistency_passed",
        "consistency_mismatched_metrics",
    }
)
_PREFIX_PROGRESSIVE_OPTIONAL_FIELDS = frozenset(
    {
        "binary_sha256",
        "analysis_sha256",
        "link",
        "analyze",
    }
)
_POSIX_DIRECTORY_FD_CONTRACT_AVAILABLE = (
    hasattr(os, "O_DIRECTORY")
    and all(function in os.supports_dir_fd for function in (os.open, os.link, os.unlink))
    and os.link in os.supports_follow_symlinks
)


def _durable_error(operation: str, exc: OSError) -> ExecutionError:
    return ExecutionError(
        "cannot durably create benchmark evidence: "
        f"operation={operation}, {describe_os_error(exc)}"
    )


def _destination_exists_error(operation: str, exc: OSError) -> ExecutionError:
    return ExecutionError(
        "durable evidence destination already exists: "
        f"operation={operation}, {describe_os_error(exc)}"
    )


def _write_fsync_close(descriptor: int, payload: bytes) -> None:
    operation = "write"
    failure: OSError | None = None
    close_failure: OSError | None = None
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "zero-byte durable evidence write")
            offset += written
        operation = "file_fsync"
        os.fsync(descriptor)
    except OSError as exc:
        failure = exc
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            close_failure = exc

    if failure is not None:
        diagnostic = str(_durable_error(operation, failure))
        if close_failure is not None:
            diagnostic += f"; close_failure=({describe_os_error(close_failure)})"
        raise ExecutionError(diagnostic) from failure
    if close_failure is not None:
        raise _durable_error("file_close", close_failure) from close_failure


def _partial_name(destination: Path) -> str:
    return f".{destination.name}.{secrets.token_hex(16)}.partial"


def _raise_with_cleanup_failures(
    primary: BaseException,
    cleanup_failures: list[tuple[str, OSError]],
) -> None:
    if cleanup_failures:
        rendered = "; ".join(
            f"operation={operation}, {describe_os_error(error)}"
            for operation, error in cleanup_failures
        )
        raise ExecutionError(f"{primary}; cleanup_failures=({rendered})") from primary
    raise primary


def _durable_create_posix(destination: Path, payload: bytes) -> None:
    if not _POSIX_DIRECTORY_FD_CONTRACT_AVAILABLE:
        raise ExecutionError(
            "cannot durably create benchmark evidence: "
            "operation=posix_capability_check, class=NotImplementedError, "
            "errno_name=UNAVAILABLE, errno_code=none"
        )
    try:
        parent = destination.parent.resolve(strict=True)
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory_fd = os.open(parent, directory_flags)
    except OSError as exc:
        raise _durable_error("directory_open", exc) from exc

    temporary_name: str | None = None
    temporary_created = False
    try:
        temporary_name = _partial_name(destination)
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        create_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(
                temporary_name,
                create_flags,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise _durable_error("temporary_create", exc) from exc
        temporary_created = True

        # A process crash naturally leaves this partial inode for fail-fast
        # audit.  A caught I/O error is cleaned below so the scheduler can seal
        # the original structured failure into an infrastructure terminal.
        _write_fsync_close(descriptor, payload)
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise _destination_exists_error("publish_linkat", exc) from exc
            raise _durable_error("publish_linkat", exc) from exc
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise _durable_error("publish_directory_fsync", exc) from exc
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except OSError as exc:
            raise _durable_error("partial_unlink", exc) from exc
        temporary_created = False
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise _durable_error("cleanup_directory_fsync", exc) from exc
    except BaseException as primary:
        cleanup_failures: list[tuple[str, OSError]] = []
        if temporary_created and temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                temporary_created = False
            except OSError as cleanup_error:
                cleanup_failures.append(("failure_partial_unlink", cleanup_error))
            else:
                temporary_created = False
            try:
                os.fsync(directory_fd)
            except OSError as cleanup_error:
                cleanup_failures.append(
                    ("failure_cleanup_directory_fsync", cleanup_error)
                )
        try:
            os.close(directory_fd)
        except OSError as close_error:
            cleanup_failures.append(("failure_directory_close", close_error))
        _raise_with_cleanup_failures(primary, cleanup_failures)
    else:
        try:
            os.close(directory_fd)
        except OSError as exc:
            raise _durable_error("directory_close", exc) from exc


def _durable_create_windows(destination: Path, payload: bytes) -> None:
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise _durable_error("directory_resolve", exc) from exc
    normalized_destination = parent / destination.name
    temporary = parent / _partial_name(destination)
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    create_flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        descriptor = os.open(temporary, create_flags, 0o600)
    except OSError as exc:
        raise _durable_error("temporary_create", exc) from exc
    temporary_created = True
    try:
        _write_fsync_close(descriptor, payload)

        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file_ex = kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        movefile_write_through = 0x00000008
        if not move_file_ex(
            str(temporary),
            str(normalized_destination),
            movefile_write_through,
        ):
            winerror = ctypes.get_last_error()
            error = ctypes.WinError(winerror)
            if winerror in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise _destination_exists_error("publish_movefileex", error) from error
            raise _durable_error("publish_movefileex", error) from error
        temporary_created = False
    except BaseException as primary:
        cleanup_failures: list[tuple[str, OSError]] = []
        if temporary_created:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_failures.append(("failure_partial_unlink", cleanup_error))
        _raise_with_cleanup_failures(primary, cleanup_failures)


def _validate_phase_result_keys(stage: str, result: Mapping[str, Any]) -> None:
    if not isinstance(result.get("case_prefix"), dict):
        raise ExecutionError("raw phase result lacks its normalized case prefix")
    keys = set(result) - {"case_prefix"}
    if stage == "compile":
        if keys != _COMPILE_RESULT_CORE and keys != _COMPILE_RESULT_SUCCESS:
            raise ExecutionError("raw compile result has an invalid field set")
    elif stage == "link":
        if keys != {"link"}:
            raise ExecutionError("raw link result has an invalid field set")
    elif stage == "analyze":
        if keys not in ({"analyze"}, {"analyze", "analysis_sha256"}):
            raise ExecutionError("raw analyzer result has an invalid field set")
    elif keys != {"sample"}:
        raise ExecutionError("raw run result has an invalid field set")


def _validate_stage_start(stage: str, completed_stages: list[str]) -> None:
    if stage == "compile":
        if completed_stages:
            raise ExecutionError("raw compile phase is duplicated or out of order")
        return
    if stage == "link":
        if completed_stages != ["compile"]:
            raise ExecutionError("raw link phase is duplicated or out of order")
        return
    if stage == "analyze":
        if completed_stages not in (["compile"], ["compile", "link"]):
            raise ExecutionError("raw analyzer phase is duplicated or out of order")
        return

    run_index = int(stage.removeprefix("run-"))
    completed_runs = [stage for stage in completed_stages if stage.startswith("run-")]
    non_run_stages = completed_stages[: len(completed_stages) - len(completed_runs)]
    if (
        non_run_stages
        not in (
            ["compile"],
            ["compile", "link"],
            ["compile", "analyze"],
            ["compile", "link", "analyze"],
        )
        or run_index != len(completed_runs)
        or completed_runs != [f"run-{index:04d}" for index in range(len(completed_runs))]
    ):
        raise ExecutionError("raw run phase is duplicated or out of order")


def _expected_terminal_stages(case_result: Mapping[str, Any]) -> list[str]:
    stages: list[str] = []
    if case_result.get("compile") is not None:
        stages.append("compile")
    if case_result.get("link") is not None:
        stages.append("link")
    if case_result.get("analyze") is not None:
        stages.append("analyze")
    samples = case_result.get("samples")
    if not isinstance(samples, list):
        raise ExecutionError("raw terminal case result lacks a samples array")
    stages.extend(f"run-{index:04d}" for index in range(len(samples)))
    return stages


def _bind_phase_results_to_terminal(
    phase_results: Mapping[str, Mapping[str, Any]],
    case_result: Mapping[str, Any],
) -> None:
    terminal_keys = set(case_result)
    prefixes: list[Mapping[str, Any]] = []
    for result in phase_results.values():
        prefix = result["case_prefix"]
        if set(prefix) != terminal_keys:
            raise ExecutionError("raw phase case prefix has an invalid field set")
        prefixes.append(prefix)
        for field in terminal_keys - _PREFIX_TERMINAL_OVERLAY_FIELDS:
            observed = prefix[field]
            terminal = case_result[field]
            if field in {"measurements", "samples"}:
                if (
                    not isinstance(observed, list)
                    or not isinstance(terminal, list)
                    or observed != terminal[: len(observed)]
                ):
                    raise ExecutionError(
                        f"raw phase case prefix is not a terminal prefix: {field}"
                    )
            elif field in _PREFIX_PROGRESSIVE_OPTIONAL_FIELDS and observed is None:
                continue
            elif observed != terminal:
                raise ExecutionError(
                    f"raw phase case prefix differs from terminal evidence: {field}"
                )
    if prefixes:
        latest = prefixes[-1]
        for field in terminal_keys - _PREFIX_TERMINAL_OVERLAY_FIELDS:
            if latest[field] != case_result[field]:
                raise ExecutionError(
                    f"raw terminal contains evidence beyond its committed phase prefix: {field}"
                )

    compile_result = phase_results.get("compile")
    if compile_result is not None:
        compile_prefix = compile_result["case_prefix"]
        expected = {
            key: compile_prefix.get(key)
            for key in (
                "compile",
                "compile_samples",
                "compile_statistics",
                "cache_hit",
            )
        }
        compile_details = {
            key: value for key, value in compile_result.items() if key != "case_prefix"
        }
        if set(compile_details) == _COMPILE_RESULT_SUCCESS:
            expected.update(
                {
                    key: compile_prefix.get(key)
                    for key in (
                        "artifact_sha256",
                        "remarks_sha256",
                        "remarks_event_count",
                        "candidate_remark_summary",
                    )
                }
            )
        if compile_details != expected:
            raise ExecutionError("raw compile result differs from terminal case evidence")

    link_result = phase_results.get("link")
    if link_result is not None:
        link_details = {
            key: value for key, value in link_result.items() if key != "case_prefix"
        }
        if link_details != {"link": link_result["case_prefix"].get("link")}:
            raise ExecutionError("raw link result differs from terminal case evidence")

    analyze_result = phase_results.get("analyze")
    if analyze_result is not None:
        analyze_details = {
            key: value for key, value in analyze_result.items() if key != "case_prefix"
        }
        analyze_prefix = analyze_result["case_prefix"]
        expected_analyze = {"analyze": analyze_prefix.get("analyze")}
        if "analysis_sha256" in analyze_details:
            expected_analyze["analysis_sha256"] = analyze_prefix.get("analysis_sha256")
        if analyze_details != expected_analyze:
            raise ExecutionError("raw analyzer result differs from terminal case evidence")

    samples = case_result.get("samples")
    assert isinstance(samples, list)
    for stage, result in phase_results.items():
        if not stage.startswith("run-"):
            continue
        index = int(stage.removeprefix("run-"))
        run_details = {key: value for key, value in result.items() if key != "case_prefix"}
        prefix_samples = result["case_prefix"].get("samples")
        if (
            index >= len(samples)
            or not isinstance(prefix_samples, list)
            or index >= len(prefix_samples)
            or run_details != {"sample": prefix_samples[index]}
        ):
            raise ExecutionError("raw run result differs from terminal sample evidence")


def _validate_scheduler_cancellation_sequence(
    case_result: Mapping[str, Any],
    completed_stages: list[str],
) -> None:
    """Validate the physical terminal cancellation independently of run schema."""
    if not completed_stages:
        return

    compile_phase = case_result.get("compile")
    compile_samples = case_result.get("compile_samples")
    if not isinstance(compile_phase, Mapping) or not isinstance(compile_samples, list):
        raise ExecutionError("scheduler cancellation lacks compile phase evidence")

    final_stage = completed_stages[-1]
    compile_statuses = [sample.get("status") for sample in compile_samples]
    if final_stage == "compile":
        if (
            compile_phase.get("status") != "cancelled"
            or not compile_statuses
            or compile_statuses[-1] != "cancelled"
            or any(status != "ok" for status in compile_statuses[:-1])
        ):
            raise ExecutionError(
                "scheduler cancellation compile terminal differs from its cold samples"
            )
        return

    if not compile_statuses or compile_phase.get("status") != "ok" or any(
        status != "ok" for status in compile_statuses
    ):
        raise ExecutionError(
            "scheduler cancellation contains a non-successful predecessor compile stage"
        )

    link_phase = case_result.get("link")
    analyze_phase = case_result.get("analyze")
    if final_stage == "link":
        final_phase = link_phase
    else:
        if link_phase is not None and link_phase.get("status") != "ok":
            raise ExecutionError(
                "scheduler cancellation contains a non-successful predecessor link stage"
            )
        if final_stage == "analyze":
            final_phase = analyze_phase
        else:
            if analyze_phase is not None and analyze_phase.get("status") != "ok":
                raise ExecutionError(
                    "scheduler cancellation contains a non-successful predecessor analyzer stage"
                )
            samples = case_result.get("samples")
            if not isinstance(samples, list) or not samples:
                raise ExecutionError("scheduler cancellation lacks its terminal run sample")
            if any(sample.get("status") != "passed" for sample in samples[:-1]):
                raise ExecutionError(
                    "scheduler cancellation contains a non-successful predecessor run sample"
                )
            final_phase = samples[-1]

    if not isinstance(final_phase, Mapping) or final_phase.get("status") != "cancelled":
        raise ExecutionError("scheduler cancellation lacks a unique cancelled terminal stage")


def _validate_terminal_contract(
    *,
    completed_stages: list[str],
    phase_results: Mapping[str, Mapping[str, Any]],
    phase_raw_files: Mapping[str, list[dict[str, Any]]],
    open_stage: str | None,
    terminal_event: Mapping[str, Any],
) -> None:
    payload = terminal_event["payload"]
    case_result = payload["case_result"]
    raw_files = payload["raw_files"]
    status = case_result.get("status")
    cancellation_reason = case_result.get("cancellation_reason")
    if status not in {
        "passed",
        "wrong_output",
        "compile_error",
        "link_error",
        "analyze_error",
        "runtime_error",
        "timeout",
        "measurement_inconsistent",
        "cancelled",
    }:
        raise ExecutionError("raw terminal case result has an invalid status")
    if status == "cancelled":
        if cancellation_reason not in {
            "scheduler_cancelled",
            "execution_interrupted",
            "infrastructure_failure",
        }:
            raise ExecutionError("raw terminal cancellation reason is invalid")
    elif cancellation_reason is not None:
        raise ExecutionError("non-cancelled terminal result carries a cancellation reason")

    _bind_phase_results_to_terminal(phase_results, case_result)
    if cancellation_reason == "infrastructure_failure":
        # Infrastructure failures are the only outcome allowed to seal an
        # in-flight phase.  Completed phases remain hash-bound above, while the
        # open phase is explicit evidence that no normal result was committed.
        phases = [
            phase
            for phase in (
                case_result.get("compile"),
                case_result.get("link"),
                case_result.get("analyze"),
            )
            if phase is not None
        ]
        if (
            any(phase.get("status") != "ok" for phase in phases)
            or any(
                sample.get("status") != "ok"
                for sample in case_result.get("compile_samples", [])
            )
            or any(
                sample.get("status") != "passed"
                for sample in case_result.get("samples", [])
            )
        ):
            raise ExecutionError(
                "infrastructure terminal contains a committed real stage failure"
            )
        return

    if open_stage is not None:
        raise ExecutionError("raw attempt journal terminal outcome has an open phase")
    expected_stages = _expected_terminal_stages(case_result)
    if completed_stages != expected_stages:
        raise ExecutionError("raw terminal phase sequence differs from terminal case evidence")

    if cancellation_reason == "execution_interrupted":
        if completed_stages:
            raise ExecutionError("pre-phase interruption contains completed phase evidence")
        return

    if status == "cancelled":
        _validate_scheduler_cancellation_sequence(case_result, completed_stages)
        return

    if status in {"passed", "measurement_inconsistent"}:
        samples = case_result["samples"]
        if (
            not completed_stages
            or completed_stages[0] != "compile"
            or not samples
            or any(sample.get("status") != "passed" for sample in samples)
            or not raw_files
        ):
            raise ExecutionError("successful terminal result lacks complete phase or raw evidence")
        for stage, event_result in phase_results.items():
            # Every completed phase must bind at least one durable raw file.
            # The inventory itself is validated by _validate_event_shape.
            if not event_result or not phase_raw_files[stage]:
                raise ExecutionError("successful terminal result contains an empty phase result")
        return

    if status == "compile_error":
        if completed_stages != ["compile"]:
            raise ExecutionError("compile failure has an invalid terminal phase sequence")
        phase = case_result.get("compile")
    elif status == "link_error":
        if not completed_stages or completed_stages[-1] != "link":
            raise ExecutionError("link failure has an invalid terminal phase sequence")
        phase = case_result.get("link")
    elif status == "analyze_error":
        if not completed_stages or completed_stages[-1] != "analyze":
            raise ExecutionError("analyzer failure has an invalid terminal phase sequence")
        phase = case_result.get("analyze")
    elif status in {"runtime_error", "wrong_output", "timeout"}:
        samples = case_result["samples"]
        if not samples or samples[-1].get("status") != status:
            raise ExecutionError("runtime terminal result differs from its final sample")
        if any(sample.get("status") != "passed" for sample in samples[:-1]):
            raise ExecutionError("runtime terminal result contains evidence after a failed sample")
        return
    if not isinstance(phase, Mapping) or phase.get("status") not in {
        "error",
        "timeout",
    }:
        raise ExecutionError("terminal failure is not supported by its final phase evidence")


def _durable_create(destination: Path, payload: bytes) -> None:
    """Publish complete immutable evidence without replacing an existing file.

    Both implementations first make an exclusive same-directory partial inode
    durable.  A crash before publication therefore leaves explicit partial
    audit evidence, while readers can never observe a truncated final event.
    POSIX publication uses ``linkat`` names anchored to one open directory
    descriptor, avoiding DrvFS path-identity drift.  Windows uses no-replace
    ``MoveFileExW`` with normalized absolute paths and write-through enabled.
    """

    if os.name == "nt":
        _durable_create_windows(destination, payload)
    else:
        _durable_create_posix(destination, payload)


def durable_create_json(destination: Path, value: Mapping[str, Any]) -> None:
    _durable_create(destination, canonical_json_bytes(value) + b"\n")


@dataclass(frozen=True)
class JournalSnapshot:
    events: tuple[dict[str, Any], ...]
    event_sha256s: tuple[str, ...]
    commitment_sha256: str

    @property
    def terminal_case_result(self) -> dict[str, Any] | None:
        if not self.events or self.events[-1]["event_type"] != "terminal":
            return None
        return self.events[-1]["payload"]["case_result"]

    @property
    def terminal_raw_files(self) -> list[dict[str, Any]] | None:
        if not self.events or self.events[-1]["event_type"] != "terminal":
            return None
        return self.events[-1]["payload"]["raw_files"]

    @property
    def has_phase_evidence(self) -> bool:
        return any(event["event_type"].startswith("phase_") for event in self.events)

    @property
    def latest_committed_case_prefix(self) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event["event_type"] == "phase_result":
                return event["payload"]["result"]["case_prefix"]
        return None


@dataclass(frozen=True)
class RunTerminalSnapshot:
    """Verified append-only terminal observations for one benchmark run."""

    events: tuple[dict[str, Any], ...]
    event_sha256s: tuple[str, ...]
    commitment_sha256: str

    @property
    def latest_terminal(self) -> dict[str, Any] | None:
        if not self.events:
            return None
        return self.events[-1]["payload"]


class RunTerminalJournal:
    """Durable run-level terminal evidence, including zero-attempt interruption.

    A run may be interrupted and safely resumed more than once, so this is an
    append-only chain rather than a replaceable ``terminal.json``. Completed
    and failed observations are final; only an interrupted observation may be
    followed by another terminal event.
    """

    _IDENTITY_KEYS = {
        "schema_version",
        "run_id",
        "manifest_sha256",
        "configuration_sha256",
        "started_at",
        "output_path_sha256",
    }
    _PAYLOAD_KEYS = {
        "state",
        "observed_at",
        "summary_sha256",
        "case_terminal_commitments_sha256",
    }

    def __init__(
        self,
        run_directory: Path,
        *,
        identity: Mapping[str, Any],
    ) -> None:
        self.run_directory = run_directory
        self.directory = run_directory / "run-terminal"
        self.identity = dict(identity)
        if (
            set(self.identity) != self._IDENTITY_KEYS
            or self.identity.get("schema_version")
            != "benchmark-run-terminal-identity.v1"
            or not isinstance(self.identity.get("run_id"), str)
            or not self.identity["run_id"]
            or any(
                not isinstance(self.identity.get(field), str)
                or _SHA256.fullmatch(self.identity[field]) is None
                for field in (
                    "manifest_sha256",
                    "configuration_sha256",
                    "output_path_sha256",
                )
            )
            or not isinstance(self.identity.get("started_at"), str)
        ):
            raise ExecutionError("run terminal journal identity is invalid")
        try:
            started = datetime.fromisoformat(
                self.identity["started_at"].replace("Z", "+00:00")
            )
            if started.utcoffset() is None:
                raise ValueError("timestamp lacks a UTC offset")
        except ValueError as exc:
            raise ExecutionError(
                "run terminal journal identity timestamp is invalid"
            ) from exc
        self.identity_sha256 = sha256_json(self.identity)

    @property
    def exists(self) -> bool:
        return self.directory.exists() or self.directory.is_symlink()

    def initialize(self) -> None:
        if self.exists:
            raise ExecutionError("run terminal journal already exists")
        try:
            self.directory.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise ExecutionError("cannot create run terminal journal") from exc
        durable_create_json(
            self.directory / "metadata.json",
            {
                "schema_version": "benchmark-run-terminal-journal.v1",
                "identity": self.identity,
                "identity_sha256": self.identity_sha256,
                "durability_contract": (
                    "posix-directory-fsync-or-windows-write-through.v1"
                ),
            },
        )

    def _validate_payload(self, payload: Any) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload) != self._PAYLOAD_KEYS
            or payload.get("state") not in {"completed", "failed", "interrupted"}
            or not isinstance(payload.get("observed_at"), str)
            or any(
                not isinstance(payload.get(field), str)
                or _SHA256.fullmatch(payload[field]) is None
                for field in (
                    "summary_sha256",
                    "case_terminal_commitments_sha256",
                )
            )
        ):
            raise ExecutionError("run terminal journal payload is invalid")
        try:
            observed = datetime.fromisoformat(
                payload["observed_at"].replace("Z", "+00:00")
            )
            started = datetime.fromisoformat(
                self.identity["started_at"].replace("Z", "+00:00")
            )
            if observed.utcoffset() is None or started.utcoffset() is None:
                raise ValueError("timestamp lacks a UTC offset")
        except ValueError as exc:
            raise ExecutionError(
                "run terminal journal payload timestamp is invalid"
            ) from exc
        if observed < started:
            raise ExecutionError(
                "run terminal journal observation predates the run"
            )

    def _validate_event(
        self,
        event: Any,
        *,
        sequence: int,
        previous_sha256: str | None,
        previous_payload: Mapping[str, Any] | None,
    ) -> None:
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise ExecutionError("run terminal journal event has an invalid object shape")
        if (
            event["schema_version"] != "benchmark-run-terminal-event.v1"
            or event["sequence"] != sequence
            or event["event_type"] != "terminal"
            or event["identity_sha256"] != self.identity_sha256
            or event["previous_event_sha256"] != previous_sha256
        ):
            raise ExecutionError("run terminal journal event identity or chain is invalid")
        self._validate_payload(event["payload"])
        if previous_payload is not None:
            if previous_payload["state"] != "interrupted":
                raise ExecutionError(
                    "run terminal journal contains evidence after a final outcome"
                )
            previous_time = datetime.fromisoformat(
                previous_payload["observed_at"].replace("Z", "+00:00")
            )
            current_time = datetime.fromisoformat(
                event["payload"]["observed_at"].replace("Z", "+00:00")
            )
            if current_time < previous_time:
                raise ExecutionError(
                    "run terminal journal observation time regressed"
                )

    def load(self) -> RunTerminalSnapshot:
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ExecutionError(
                "run terminal journal is missing or is not a directory"
            )
        try:
            entries = list(self.directory.iterdir())
        except OSError as exc:
            raise ExecutionError("cannot enumerate run terminal journal") from exc
        metadata_path = self.directory / "metadata.json"
        if metadata_path not in entries:
            raise ExecutionError("run terminal journal metadata is missing")
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ExecutionError("run terminal journal metadata is not a regular file")
        try:
            metadata = read_json(metadata_path)
        except ValidationError as exc:
            raise ExecutionError("run terminal journal metadata is invalid") from exc
        if metadata != {
            "schema_version": "benchmark-run-terminal-journal.v1",
            "identity": self.identity,
            "identity_sha256": self.identity_sha256,
            "durability_contract": (
                "posix-directory-fsync-or-windows-write-through.v1"
            ),
        }:
            raise ExecutionError("run terminal journal metadata identity differs")

        indexed: list[tuple[int, str, Path]] = []
        for path in entries:
            if path == metadata_path:
                continue
            match = _EVENT_FILE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise ExecutionError(
                    "run terminal journal contains an unexpected entry"
                )
            indexed.append((int(match.group("sequence")), match.group("sha256"), path))
        indexed.sort(key=lambda item: item[0])
        if [item[0] for item in indexed] != list(range(len(indexed))):
            raise ExecutionError(
                "run terminal journal event sequence is not contiguous"
            )

        events: list[dict[str, Any]] = []
        hashes: list[str] = []
        previous_sha256: str | None = None
        previous_payload: Mapping[str, Any] | None = None
        for sequence, expected_sha256, path in indexed:
            try:
                encoded = path.read_bytes()
            except OSError as exc:
                raise ExecutionError("cannot read run terminal journal event") from exc
            if sha256_bytes(encoded) != expected_sha256:
                raise ExecutionError(
                    "run terminal journal event content hash differs"
                )
            try:
                event = read_json(path)
            except ValidationError as exc:
                raise ExecutionError(
                    "run terminal journal event is not valid JSON"
                ) from exc
            self._validate_event(
                event,
                sequence=sequence,
                previous_sha256=previous_sha256,
                previous_payload=previous_payload,
            )
            events.append(event)
            hashes.append(expected_sha256)
            previous_sha256 = expected_sha256
            previous_payload = event["payload"]

        commitment = sha256_json(
            {
                "schema_version": "benchmark-run-terminal-journal-commitment.v1",
                "identity_sha256": self.identity_sha256,
                "event_sha256s": hashes,
            }
        )
        return RunTerminalSnapshot(tuple(events), tuple(hashes), commitment)

    def seal(self, payload: Mapping[str, Any]) -> RunTerminalSnapshot:
        if not self.exists:
            self.initialize()
        snapshot = self.load()
        normalized_payload = dict(payload)
        self._validate_payload(normalized_payload)
        if snapshot.latest_terminal == normalized_payload:
            return snapshot
        if snapshot.latest_terminal is not None and snapshot.latest_terminal[
            "state"
        ] != "interrupted":
            raise ExecutionError("cannot append evidence after final run outcome")
        previous_sha256 = (
            snapshot.event_sha256s[-1] if snapshot.event_sha256s else None
        )
        event = {
            "schema_version": "benchmark-run-terminal-event.v1",
            "sequence": len(snapshot.events),
            "event_type": "terminal",
            "identity_sha256": self.identity_sha256,
            "previous_event_sha256": previous_sha256,
            "payload": normalized_payload,
        }
        self._validate_event(
            event,
            sequence=len(snapshot.events),
            previous_sha256=previous_sha256,
            previous_payload=snapshot.latest_terminal,
        )
        encoded = canonical_json_bytes(event) + b"\n"
        digest = sha256_bytes(encoded)
        _durable_create(
            self.directory / f"event-{len(snapshot.events):04d}-{digest}.json",
            encoded,
        )
        return self.load()


class AttemptJournal:
    """Append-only, hash-chained raw evidence for one benchmark attempt."""

    def __init__(self, attempt_directory: Path, *, identity_sha256: str) -> None:
        self.attempt_directory = attempt_directory
        self.directory = attempt_directory / "journal"
        self.identity_sha256 = identity_sha256

    def initialize(self) -> None:
        try:
            self.directory.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise ExecutionError("cannot create raw attempt journal") from exc
        durable_create_json(
            self.directory / "metadata.json",
            {
                "schema_version": "benchmark-attempt-journal.v1",
                "identity_sha256": self.identity_sha256,
                "durability_contract": "posix-directory-fsync-or-windows-write-through.v1",
            },
        )

    def _raw_files(self) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for path in self.attempt_directory.rglob("*"):
            relative = path.relative_to(self.attempt_directory).as_posix()
            if relative == "identity.json" or relative == "journal" or relative.startswith(
                "journal/"
            ):
                continue
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ExecutionError("raw attempt evidence contains a non-regular entry")
            if path.is_file():
                files.append(
                    {
                        "path": relative,
                        "sha256": sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
        files.sort(key=lambda item: item["path"].encode("utf-8"))
        return files

    def assert_no_uncommitted_raw_evidence(self) -> None:
        entries = {path.name for path in self.attempt_directory.iterdir()}
        if entries != {"identity.json", "journal"}:
            raise ExecutionError(
                "empty attempt journal is accompanied by uncommitted raw phase evidence"
            )

    def verify_terminal_raw_files(self, snapshot: JournalSnapshot) -> None:
        expected = snapshot.terminal_raw_files
        if expected is None or expected != self._raw_files():
            raise ExecutionError("raw attempt files differ from the terminal journal inventory")

    def _validate_event_shape(
        self,
        event: Any,
        *,
        sequence: int,
        previous_sha256: str | None,
    ) -> None:
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise ExecutionError("raw attempt journal event has an invalid object shape")
        if (
            event["schema_version"] != "benchmark-attempt-journal-event.v1"
            or event["sequence"] != sequence
            or event["identity_sha256"] != self.identity_sha256
            or event["previous_event_sha256"] != previous_sha256
            or event["event_type"] not in {"phase_started", "phase_result", "terminal"}
            or not isinstance(event["payload"], dict)
        ):
            raise ExecutionError("raw attempt journal event identity or chain is invalid")
        payload = event["payload"]
        if event["event_type"] == "phase_started":
            if set(payload) != {"stage"} or not isinstance(payload["stage"], str):
                raise ExecutionError("raw phase-start journal payload is invalid")
        elif event["event_type"] == "phase_result":
            if (
                set(payload) != {"stage", "result", "raw_files"}
                or not isinstance(payload["stage"], str)
                or not isinstance(payload["result"], dict)
                or not isinstance(payload["raw_files"], list)
            ):
                raise ExecutionError("raw phase-result journal payload is invalid")
            _validate_phase_result_keys(payload["stage"], payload["result"])
        elif (
            set(payload) != {"case_result", "raw_files"}
            or not isinstance(payload["case_result"], dict)
            or not isinstance(payload["raw_files"], list)
        ):
            raise ExecutionError("raw terminal journal payload is invalid")
        stage = payload.get("stage")
        if stage is not None and not _STAGE_ID.fullmatch(stage):
            raise ExecutionError("raw attempt journal contains an invalid stage id")
        raw_files = payload.get("raw_files")
        if raw_files is not None:
            previous_path: bytes | None = None
            for item in raw_files:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"path", "sha256", "size_bytes"}
                    or not isinstance(item["path"], str)
                    or not item["path"]
                    or "\\" in item["path"]
                    or item["path"].startswith("/")
                    or any(part in {"", ".", ".."} for part in item["path"].split("/"))
                    or item["path"] == "identity.json"
                    or item["path"].startswith("journal/")
                    or not isinstance(item["sha256"], str)
                    or _SHA256.fullmatch(item["sha256"]) is None
                    or not isinstance(item["size_bytes"], int)
                    or isinstance(item["size_bytes"], bool)
                    or item["size_bytes"] < 0
                ):
                    raise ExecutionError("raw attempt journal file inventory is invalid")
                encoded_path = item["path"].encode("utf-8")
                if previous_path is not None and encoded_path <= previous_path:
                    raise ExecutionError("raw attempt journal file inventory is not strictly ordered")
                previous_path = encoded_path

    def load(self) -> JournalSnapshot:
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ExecutionError("raw attempt journal is missing or is not a directory")
        indexed: list[tuple[int, str, Path]] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError as exc:
            raise ExecutionError("cannot enumerate raw attempt journal") from exc
        metadata_path = self.directory / "metadata.json"
        if metadata_path not in entries:
            raise ExecutionError("raw attempt journal metadata is missing")
        try:
            metadata = read_json(metadata_path)
        except ValidationError as exc:
            raise ExecutionError("raw attempt journal metadata is invalid") from exc
        if metadata != {
            "schema_version": "benchmark-attempt-journal.v1",
            "identity_sha256": self.identity_sha256,
            "durability_contract": "posix-directory-fsync-or-windows-write-through.v1",
        }:
            raise ExecutionError("raw attempt journal metadata identity differs")
        for path in entries:
            if path == metadata_path:
                if path.is_symlink() or not path.is_file():
                    raise ExecutionError("raw attempt journal metadata is not a regular file")
                continue
            match = _EVENT_FILE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise ExecutionError("raw attempt journal contains an unexpected entry")
            indexed.append((int(match.group("sequence")), match.group("sha256"), path))
        indexed.sort(key=lambda item: item[0])
        if [item[0] for item in indexed] != list(range(len(indexed))):
            raise ExecutionError("raw attempt journal event sequence is not contiguous")

        events: list[dict[str, Any]] = []
        hashes: list[str] = []
        previous_sha256: str | None = None
        open_stage: str | None = None
        completed_stages: list[str] = []
        phase_results: dict[str, Mapping[str, Any]] = {}
        phase_raw_files: dict[str, list[dict[str, Any]]] = {}
        terminal_seen = False
        terminal_event: Mapping[str, Any] | None = None
        for sequence, expected_sha256, path in indexed:
            try:
                payload_bytes = path.read_bytes()
            except OSError as exc:
                raise ExecutionError("cannot read raw attempt journal event") from exc
            if sha256_bytes(payload_bytes) != expected_sha256:
                raise ExecutionError("raw attempt journal event content hash differs")
            try:
                event = read_json(path)
            except ValidationError as exc:
                raise ExecutionError("raw attempt journal event is not valid JSON") from exc
            self._validate_event_shape(
                event,
                sequence=sequence,
                previous_sha256=previous_sha256,
            )
            event_type = event["event_type"]
            if terminal_seen:
                raise ExecutionError("raw attempt journal contains evidence after terminal outcome")
            if event_type == "phase_started":
                if open_stage is not None:
                    raise ExecutionError("raw attempt journal has overlapping phases")
                stage = event["payload"]["stage"]
                _validate_stage_start(stage, completed_stages)
                open_stage = stage
            elif event_type == "phase_result":
                if open_stage != event["payload"]["stage"]:
                    raise ExecutionError("raw attempt journal phase result lacks its matching start")
                completed_stages.append(open_stage)
                phase_results[open_stage] = event["payload"]["result"]
                phase_raw_files[open_stage] = event["payload"]["raw_files"]
                open_stage = None
            else:
                terminal_seen = True
                terminal_event = event
            events.append(event)
            hashes.append(expected_sha256)
            previous_sha256 = expected_sha256

        if terminal_event is not None:
            _validate_terminal_contract(
                completed_stages=completed_stages,
                phase_results=phase_results,
                phase_raw_files=phase_raw_files,
                open_stage=open_stage,
                terminal_event=terminal_event,
            )

        commitment = sha256_json(
            {
                "schema_version": "benchmark-attempt-journal-commitment.v1",
                "identity_sha256": self.identity_sha256,
                "event_sha256s": hashes,
            }
        )
        return JournalSnapshot(tuple(events), tuple(hashes), commitment)

    def _append(self, event_type: str, payload: Mapping[str, Any]) -> JournalSnapshot:
        snapshot = self.load()
        if snapshot.terminal_case_result is not None:
            raise ExecutionError("cannot append evidence after terminal attempt outcome")
        open_stage: str | None = None
        completed_stages: list[str] = []
        phase_results: dict[str, Mapping[str, Any]] = {}
        phase_raw_files: dict[str, list[dict[str, Any]]] = {}
        for existing in snapshot.events:
            if existing["event_type"] == "phase_started":
                open_stage = existing["payload"]["stage"]
            elif existing["event_type"] == "phase_result":
                stage = existing["payload"]["stage"]
                completed_stages.append(stage)
                phase_results[stage] = existing["payload"]["result"]
                phase_raw_files[stage] = existing["payload"]["raw_files"]
                open_stage = None
        if event_type == "phase_started" and open_stage is not None:
            raise ExecutionError("cannot start an overlapping raw attempt phase")
        if event_type == "phase_started":
            stage = payload.get("stage")
            if not isinstance(stage, str) or _STAGE_ID.fullmatch(stage) is None:
                raise ExecutionError("cannot start a raw attempt phase with an invalid id")
            _validate_stage_start(stage, completed_stages)
        if event_type == "phase_result" and payload.get("stage") != open_stage:
            raise ExecutionError("cannot commit a phase result without its matching start")
        if event_type == "terminal" and open_stage is not None:
            case_result = payload.get("case_result")
            if not isinstance(case_result, Mapping) or not (
                case_result.get("status") == "cancelled"
                and case_result.get("cancellation_reason") == "infrastructure_failure"
            ):
                raise ExecutionError("cannot commit a terminal result while a phase is open")
        event_payload = dict(payload)
        if event_type in {"phase_result", "terminal"}:
            event_payload["raw_files"] = self._raw_files()
        event = {
            "schema_version": "benchmark-attempt-journal-event.v1",
            "sequence": len(snapshot.events),
            "event_type": event_type,
            "identity_sha256": self.identity_sha256,
            "previous_event_sha256": (
                snapshot.event_sha256s[-1] if snapshot.event_sha256s else None
            ),
            "payload": event_payload,
        }
        self._validate_event_shape(
            event,
            sequence=len(snapshot.events),
            previous_sha256=(snapshot.event_sha256s[-1] if snapshot.event_sha256s else None),
        )
        if event_type == "terminal":
            _validate_terminal_contract(
                completed_stages=completed_stages,
                phase_results=phase_results,
                phase_raw_files=phase_raw_files,
                open_stage=open_stage,
                terminal_event=event,
            )
        encoded = canonical_json_bytes(event) + b"\n"
        digest = sha256_bytes(encoded)
        destination = self.directory / f"event-{len(snapshot.events):04d}-{digest}.json"
        _durable_create(destination, encoded)
        return self.load()

    def append_phase_started(self, stage: str) -> JournalSnapshot:
        return self._append("phase_started", {"stage": stage})

    def append_phase_result(self, stage: str, result: Mapping[str, Any]) -> JournalSnapshot:
        return self._append("phase_result", {"stage": stage, "result": dict(result)})

    def append_terminal(self, case_result: Mapping[str, Any]) -> JournalSnapshot:
        return self._append("terminal", {"case_result": dict(case_result)})
