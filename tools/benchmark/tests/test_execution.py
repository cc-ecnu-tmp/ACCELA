from __future__ import annotations

import errno
import json
import multiprocessing
import os
import sys
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from tools.benchmark import cache as cache_module
from tools.benchmark import execution as execution_module
from tools.benchmark import journal as journal_module
from tools.benchmark.analyzer_contract import (
    FORMAL_ANALYZER_MODULE,
    FORMAL_ANALYZER_TOOLCHAINS,
    candidate_analyzer_contract,
)
from tools.benchmark.candidate_workspace import VENV_PATH
from tools.benchmark.execution import (
    BenchmarkRun,
    MeasurementSpec,
    _validate_options,
    _summary,
    run_benchmark,
    verify_run_raw_evidence,
    verify_run_raw_evidence_read_only_snapshot,
)
from tools.benchmark.errors import ConfigurationError, ExecutionError, ValidationError
from tools.benchmark.journal import AttemptJournal
from tools.benchmark.lease import ExclusiveFileLease, output_lease_path
from tools.benchmark.adapters import StageSpec
from tools.benchmark.schema import load_and_validate, validate_document
from tools.benchmark.protocol import capture_measurement_protocol
from tools.benchmark.process import run_process
from tools.benchmark.util import atomic_write_json, sha256_file, sha256_json


class InjectedCommittedPhaseError(RuntimeError):
    pass


class InjectedTerminalPublishError(RuntimeError):
    pass


def _with_formal_authorization(options, *, task_id: str):
    return replace(
        options,
        compiler_artifact_path=options.workspace_root / "fixture_tool.py",
        candidate_campaign_plan_path=options.manifest_path,
        candidate_campaign_status_path=options.manifest_path,
        candidate_status_ledger_paths=(options.manifest_path,),
        candidate_task_id=task_id,
    )


def test_formal_analyzer_contract_is_canonical_and_hash_bound() -> None:
    contract = candidate_analyzer_contract()
    assert tuple(contract["commands"]) == FORMAL_ANALYZER_TOOLCHAINS
    for toolchain, command in contract["commands"].items():
        argv = command["argv"]
        assert argv[:4] == [
            f"{VENV_PATH}/bin/python",
            "-I",
            "-m",
            FORMAL_ANALYZER_MODULE,
        ]
        assert argv[argv.index("--timeout") + 1] == "60"
        assert ("--remarks" in argv) == (toolchain == "accela")
        assert command["command_sha256"] == sha256_json(
            {"command": argv, "environment": {}}
        )


def test_execution_environment_provenance_round_trips_and_rejects_placeholders(
    benchmark_fixture,
) -> None:
    *_unused, make_options = benchmark_fixture
    options = make_options(
        output_name="environment-provenance.json",
        run_id="environment-provenance",
        additional_metrics=(
            MeasurementSpec("elf_text_bytes", "analyzer", "bytes"),
        ),
    )
    assert options.provenance.as_record() == {
        "repo_commit": options.provenance.repo_commit,
        "repo_dirty": options.provenance.repo_dirty,
        "tracked_diff_sha256": options.provenance.tracked_diff_sha256,
        "pipeline_profile_id": options.provenance.pipeline_profile_id,
        "pipeline_profile_sha256": options.provenance.pipeline_profile_sha256,
        "compiler_artifact_sha256": (
            options.provenance.compiler_artifact_sha256
        ),
        "measurement_protocol_id": options.provenance.measurement_protocol_id,
        "measurement_protocol_sha256": (
            options.provenance.measurement_protocol_sha256
        ),
    }
    analyzer_contract = candidate_analyzer_contract()
    analyzer_argv = tuple(analyzer_contract["commands"]["accela"]["argv"])
    bound = _with_formal_authorization(
        replace(
            options,
            analyzer=StageSpec("analyzer", "host", analyzer_argv, {}),
            analysis_file="analysis/binary.json",
            provenance=replace(
                options.provenance,
                execution_environment_sha256="a" * 64,
            ),
        ),
        task_id="run.B3.fixture",
    )
    _validate_options(bound)
    assert bound.provenance.as_record()["execution_environment_sha256"] == "a" * 64
    with pytest.raises(ConfigurationError, match="complete ledger"):
        _validate_options(replace(bound, candidate_status_ledger_paths=()))
    with pytest.raises(ConfigurationError, match="formal campaign authorization"):
        _validate_options(
            replace(
                options,
                candidate_registry_path=options.manifest_path,
            )
        )
    wrong_argv = list(analyzer_argv)
    wrong_argv[1] = "-E"
    for index, analyzer in enumerate(
        (
            StageSpec("analyzer", "host", tuple(wrong_argv), {}),
            StageSpec("analyzer", "wsl", analyzer_argv, {}),
            StageSpec(
                "analyzer",
                "host",
                analyzer_argv,
                {"PYTHONPATH": "ambient-modules"},
            ),
        )
    ):
        wrong_analyzer = replace(
            bound,
            analyzer=analyzer,
            output_path=bound.output_path.with_name(f"wrong-analyzer-{index}.json"),
            state_root=bound.state_root.with_name(f"wrong-analyzer-state-{index}"),
        )
        with pytest.raises(ConfigurationError, match="analyzer contract differs"):
            run_benchmark(wrong_analyzer)
        assert not wrong_analyzer.output_path.exists()
        assert not wrong_analyzer.state_root.exists()
    for invalid in ("0" * 64, "A" * 64, "short"):
        with pytest.raises(ConfigurationError, match="nonzero SHA-256"):
            _validate_options(
                replace(
                    options,
                    provenance=replace(
                        options.provenance,
                        execution_environment_sha256=invalid,
                    ),
                )
            )


def test_formal_candidate_authorization_fails_before_state_output_or_lease(
    benchmark_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_unused, make_options = benchmark_fixture
    options = make_options(
        output_name="unauthorized-formal.json",
        run_id="unauthorized-formal",
        additional_metrics=(
            MeasurementSpec("elf_text_bytes", "analyzer", "bytes"),
        ),
    )
    analyzer = StageSpec(
        "analyzer",
        "host",
        tuple(candidate_analyzer_contract()["commands"]["accela"]["argv"]),
        {},
    )
    bound = _with_formal_authorization(
        replace(
            options,
            analyzer=analyzer,
            analysis_file="analysis/binary.json",
            provenance=replace(
                options.provenance,
                execution_environment_sha256="a" * 64,
            ),
        ),
        task_id="run.B3.fixture",
    )

    def reject(_intent: object) -> None:
        raise ValidationError("injected pre-lease authorization rejection")

    monkeypatch.setattr(
        execution_module,
        "authorize_candidate_run_prelease",
        reject,
    )
    lease_path = output_lease_path(bound.output_path)
    assert not bound.output_path.exists()
    assert not bound.state_root.exists()
    assert not lease_path.exists()
    with pytest.raises(ValidationError, match="pre-lease authorization"):
        BenchmarkRun(bound)
    assert not bound.output_path.exists()
    assert not bound.state_root.exists()
    assert not lease_path.exists()


def test_formal_b1_analyzer_is_absent_and_fails_before_state_creation(
    benchmark_fixture,
) -> None:
    *_unused, make_options = benchmark_fixture
    options = make_options(output_name="formal-b1.json", run_id="formal-b1")
    options = _with_formal_authorization(
        replace(
            options,
            evidence_level="qemu_correctness",
            provenance=replace(
                options.provenance,
                execution_environment_sha256="a" * 64,
            ),
        ),
        task_id="run.B1.fixture",
    )
    _validate_options(options)
    analyzer = candidate_analyzer_contract()["commands"]["accela"]
    wrong = replace(
        options,
        analyzer=StageSpec(
            "analyzer", "host", tuple(analyzer["argv"]), {}
        ),
        state_root=options.state_root.with_name("formal-b1-wrong-state"),
    )
    with pytest.raises(ConfigurationError, match="analyzer contract differs"):
        run_benchmark(wrong)
    assert not wrong.output_path.exists()
    assert not wrong.state_root.exists()


@pytest.mark.parametrize(
    ("profile_id", "toolchain"),
    (("gcc-13.3-o2", "gcc"), ("clang-18-o3", "clang")),
)
def test_formal_reference_analyzer_is_exact_and_fails_before_state_creation(
    benchmark_fixture,
    profile_id: str,
    toolchain: str,
) -> None:
    *_unused, make_options = benchmark_fixture
    options = make_options(
        output_name=f"formal-{toolchain}.json",
        run_id=f"formal-{toolchain}",
        additional_metrics=(
            MeasurementSpec("elf_text_bytes", "analyzer", "bytes"),
        ),
    )
    command = candidate_analyzer_contract()["commands"][toolchain]
    options = _with_formal_authorization(
        replace(
            options,
            compiler=StageSpec(
                "external",
                options.compiler.adapter,
                options.compiler.command,
                options.compiler.environment,
            ),
            analyzer=StageSpec("analyzer", "host", tuple(command["argv"]), {}),
            analysis_file="analysis/binary.json",
            provenance=replace(
                options.provenance,
                pipeline_profile_id=profile_id,
                execution_environment_sha256="a" * 64,
            ),
        ),
        task_id=f"run.B3.{toolchain}",
    )
    _validate_options(options)
    wrong_argv = list(command["argv"])
    wrong_argv[-1] = "{wrong_analysis_file}"
    wrong = replace(
        options,
        analyzer=StageSpec("analyzer", "host", tuple(wrong_argv), {}),
        state_root=options.state_root.with_name(f"formal-{toolchain}-wrong-state"),
    )
    with pytest.raises(ConfigurationError, match="analyzer contract differs"):
        run_benchmark(wrong)
    assert not wrong.output_path.exists()
    assert not wrong.state_root.exists()


def _run_benchmark_in_subprocess(options: Any, result_queue: Any) -> None:
    try:
        record = run_benchmark(options)
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("ok", record["state"], record["run_id"]))


def _raw_attempt_directory(state_root: Path, case_id: str, attempt_index: int) -> Path:
    matches: list[Path] = []
    for identity_path in state_root.glob("runs/*/cases/*/attempts/attempt-*/identity.json"):
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity["case_id"] == case_id and identity["attempt_index"] == attempt_index:
            matches.append(identity_path.parent)
    assert len(matches) == 1
    return matches[0]


def _clear_case_to_uncommitted_attempt(case: dict[str, Any]) -> None:
    case.update(
        status="pending",
        cancellation_reason=None,
        cache_hit=False,
        artifact_sha256=None,
        binary_sha256=None,
        remarks_sha256=None,
        remarks_event_count=None,
        candidate_remark_summary=None,
        analysis_sha256=None,
        attempt_journal_sha256=None,
        attempt_journal_event_count=None,
        compile=None,
        compile_samples=[],
        compile_statistics=None,
        link=None,
        analyze=None,
        measurements=[],
        samples=[],
        consistency_passed=(False if case["consistency_selected"] else None),
        diagnostic="simulated interruption",
    )


def _reserve_first_attempt(options: Any) -> tuple[BenchmarkRun, dict[str, Any], Path]:
    runner = BenchmarkRun(options)
    record = runner._initial_record()
    run_directory = runner._run_directory(record)
    run_directory.mkdir(parents=True)
    runner._bind_state_identity(record, run_directory)
    attempt_directory = runner._reserve_attempt(record, record["cases"][0], run_directory)
    runner._seal_run_terminal(record, run_directory, state="interrupted")
    return runner, record, attempt_directory


def _ensure_execution_leases(options: Any, run_directory: Path) -> None:
    output_lock = output_lease_path(options.output_path)
    output_lock.parent.mkdir(parents=True, exist_ok=True)
    output_lock.touch(exist_ok=True)
    (run_directory / ".run.lock").touch(exist_ok=True)


def _rebind_protocol(options, *, runner=None, assets=None, name: str):
    bound_runner = runner or options.runner
    bound_assets = dict(options.measurement_protocol_assets) if assets is None else dict(assets)
    previous = load_and_validate(options.measurement_protocol_path)
    protocol = capture_measurement_protocol(
        protocol_id=previous["protocol_id"],
        workspace_root=options.workspace_root,
        assets=bound_assets,
        runner=bound_runner,
        machine=previous["qemu"]["machine"],
        cpu_model=previous["qemu"]["cpu_model"],
        memory=previous["qemu"]["memory"],
        measurement_mode=previous["measurement_mode"],
    )
    protocol_path = options.workspace_root / f"{name}-{sha256_json(protocol)[:12]}.json"
    atomic_write_json(protocol_path, protocol)
    return replace(
        options,
        runner=bound_runner,
        measurement_protocol_path=protocol_path,
        measurement_protocol_assets=tuple(bound_assets.items()),
        provenance=replace(
            options.provenance,
            measurement_protocol_id=protocol["protocol_id"],
            measurement_protocol_sha256=sha256_json(protocol),
        ),
    )


def test_run_process_reports_scheduler_cancellation_as_a_typed_status(tmp_path: Path) -> None:
    cancellation = threading.Event()
    cancellation.set()
    result = run_process(
        (sys.executable, "-c", "raise SystemExit(91)"),
        cwd=tmp_path,
        environment=None,
        stdin_path=None,
        stdout_path=tmp_path / "cancelled.stdout",
        stderr_path=tmp_path / "cancelled.stderr",
        timeout_seconds=1.0,
        privacy_roots=(tmp_path,),
        cancellation_event=cancellation,
    )
    assert result.status == "cancelled"
    assert result.exit_code is None
    assert result.diagnostic == "process cancelled by benchmark scheduler"


@pytest.mark.parametrize(
    ("stage_name", "failure_status"),
    (
        ("compile", "compile_error"),
        ("link", "link_error"),
        ("analyze", "analyze_error"),
        ("run", "runtime_error"),
    ),
)
def test_scheduler_cancellation_is_not_misclassified_by_any_stage(
    benchmark_fixture,
    stage_name: str,
    failure_status: str,
) -> None:
    _, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name=f"cancel-{stage_name}.json",
        run_id=f"cancel-{stage_name}",
    )
    options = replace(base, max_workers=2, keep_going=False)
    if stage_name == "compile":
        options = replace(
            options,
            compiler=replace(
                options.compiler,
                command=(
                    sys.executable,
                    str(tool),
                    "compile-controlled",
                    "{source}",
                    "{artifact}",
                ),
            ),
        )
    elif stage_name == "link":
        options = replace(
            options,
            linker=StageSpec(
                "external",
                "host",
                (
                    sys.executable,
                    str(tool),
                    "link-controlled",
                    "{artifact}",
                    "{binary}",
                ),
                {},
            ),
        )
    elif stage_name == "analyze":
        options = replace(
            options,
            analyzer=StageSpec(
                "analyzer",
                "host",
                (
                    sys.executable,
                    str(tool),
                    "analyze-controlled",
                    "{binary}",
                    "{analysis_file}",
                ),
                {},
            ),
            analysis_file="analysis/binary.json",
            additional_metrics=(
                MeasurementSpec("elf_text_bytes", "analyzer", "bytes"),
            ),
        )
    else:
        runner = StageSpec(
            "qemu",
            "host",
            (
                sys.executable,
                "{runner_executable}",
                "run-controlled",
                "{binary}",
                "{qemu_binary}",
                "{profile_plugin_binary}",
                "{cache_plugin_binary}",
                "{input}",
                "{metric_file}",
            ),
            {},
        )
        options = _rebind_protocol(options, runner=runner, name="cancel-run-protocol")

    record = run_benchmark(options)
    assert record["cases"][0]["status"] == failure_status
    cancelled = record["cases"][1]
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "scheduler_cancelled"
    if stage_name == "run":
        assert cancelled["samples"][0]["status"] == "cancelled"
    else:
        assert cancelled[stage_name]["status"] == "cancelled"
    assert "cancelled by benchmark scheduler" in cancelled["diagnostic"]


def test_second_orchestrator_fails_fast_on_output_and_execution_state_leases(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, tool, make_options = benchmark_fixture
    ready = tmp_path / "compiler.ready"
    release = tmp_path / "compiler.release"
    base = make_options(
        output_name="leased.json",
        run_id="leased-run",
        compile_repetitions=1,
    )
    options = replace(
        base,
        compiler=replace(
            base.compiler,
            command=(
                sys.executable,
                str(tool),
                "compile-gated",
                "{source}",
                "{artifact}",
                str(ready),
                str(release),
            ),
        ),
        compile_timeout_seconds=25,
        max_workers=1,
    )
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_run_benchmark_in_subprocess,
        args=(options, result_queue),
    )
    output_locks: list[Path] = []
    state_locks: list[Path] = []
    process.start()
    try:
        deadline = time.monotonic() + 15
        while not ready.is_file() and time.monotonic() < deadline:
            if not process.is_alive():
                break
            time.sleep(0.02)
        assert ready.is_file()

        started = time.monotonic()
        different_output_identity = replace(options, run_id="different-run-id")
        with pytest.raises(ExecutionError, match="output target is already owned"):
            run_benchmark(different_output_identity)
        assert time.monotonic() - started < 2

        second_output = tmp_path / "leased-second-output.json"
        second_options = replace(options, output_path=second_output)
        with pytest.raises(ExecutionError, match="execution state is already owned"):
            run_benchmark(second_options)
        assert not second_output.exists()

        output_locks = list((tmp_path / ".accela-benchmark-locks").glob("*.lock"))
        state_locks = list((tmp_path / "state" / "runs").glob("*/.run.lock"))
        assert output_locks and len(state_locks) == 1
    finally:
        release.write_bytes(b"release\n")
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert process.exitcode == 0
    try:
        child_result = result_queue.get(timeout=5)
    except Empty as exc:
        raise AssertionError("benchmark subprocess did not report its result") from exc
    assert child_result == ("ok", "completed", "leased-run")
    with pytest.raises(ExecutionError, match="already bound to another output"):
        run_benchmark(second_options)
    assert not second_output.exists()
    for lock_path in [*output_locks, *state_locks]:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        rendered = json.dumps(metadata, sort_keys=True)
        assert metadata["schema_version"] == "benchmark-run-lock.v1"
        assert metadata["run_id"] == "leased-run"
        assert str(tmp_path) not in rendered


def test_multimetric_exact_bytes_cache_and_consistency(benchmark_fixture) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    metrics = (
        MeasurementSpec("dynamic_load_count", "stderr", "instructions", r"loads=(?P<value>\d+)"),
        MeasurementSpec("dynamic_store_count", "stderr", "instructions", r"stores=(?P<value>\d+)"),
        MeasurementSpec("l1d_miss_count", "stderr", "misses", r"l1d=(?P<value>\d+)"),
        MeasurementSpec("static_instructions", "compile_stderr", "instructions", r"static=(?P<value>\d+)"),
        MeasurementSpec("spill_count", "compile_stderr", "spills", r"spills=(?P<value>\d+)"),
        MeasurementSpec("compile_time_ns", "compile_time", "ns"),
        MeasurementSpec("binary_size_bytes", "binary_size", "bytes"),
    )
    first = run_benchmark(make_options(additional_metrics=metrics, compile_repetitions=5))
    assert first["state"] == "completed"
    assert first["summary"]["passed_cases"] == 10
    assert first["summary"]["consistency_selected_cases"] == 1
    assert first["summary"]["consistency_passed_cases"] == 1
    versions = {item["tool"]: item for item in first["configuration"]["tool_versions"]}
    assert versions["riscv-gcc"]["comparison"] == "mismatch"
    assert first["configuration"]["environment_label"] == "local_reference"
    assert first["provenance"]["repo_commit"] == "1" * 40
    assert first["provenance"]["pipeline_profile_id"] == "fixture-profile"
    assert len(first["provenance"]["compiler_artifact_sha256"]) == 64
    assert all(len(case["compile_samples"]) == 5 for case in first["cases"])
    assert all(case["compile_statistics"]["sample_count"] == 5 for case in first["cases"])
    assert all(case["compile_statistics"]["mad_duration_ns"] >= 0 for case in first["cases"])
    selected = [case for case in first["cases"] if case["consistency_selected"]]
    assert len(selected) == 1 and len(selected[0]["samples"]) == 3
    assert all(len(case["samples"]) == 1 for case in first["cases"] if not case["consistency_selected"])
    case_metric_ids = {item["metric_id"] for item in first["cases"][0]["measurements"]}
    assert {"static_instructions", "spill_count", "compile_time_ns", "binary_size_bytes"} <= case_metric_ids
    sample_metric_ids = {item["metric_id"] for item in first["cases"][0]["samples"][0]["measurements"]}
    assert {"dynamic_instruction_count", "dynamic_load_count", "dynamic_store_count", "l1d_miss_count"} <= sample_metric_ids
    assert all(case["samples"][0]["stdout"]["sha256"] == case["expected_output_sha256"] for case in first["cases"])
    assert first["configuration"]["compile_storage_contract"] == "attempt_local_v1"
    assert not (tmp_path / "state" / "cache" / "compile").exists()
    attempt_directories = [
        _raw_attempt_directory(tmp_path / "state", case["case_id"], 0)
        for case in first["cases"]
    ]
    assert len(set(attempt_directories)) == len(first["cases"])
    for attempt_directory in attempt_directories:
        compile_directory = attempt_directory / "attempt-local-compile"
        assert (compile_directory / "artifact.s").is_file()
        assert (attempt_directory / "binary.elf").is_file()
        assert len(list(compile_directory.glob("repetition-*/artifact.s"))) == 5
    inconsistent_storage = deepcopy(first)
    inconsistent_storage["configuration"]["compile_storage_contract"] = "reusable_cache_v2"
    inconsistent_storage["configuration_sha256"] = sha256_json(
        {
            "configuration": inconsistent_storage["configuration"],
            "provenance": inconsistent_storage["provenance"],
        }
    )
    with pytest.raises(ValidationError, match="compile storage contract disagrees"):
        validate_document(inconsistent_storage)

    populate_options = replace(
        make_options(
            output_name="cache-populate.json",
            additional_metrics=metrics,
            compile_repetitions=5,
        ),
        run_id="cache-populate-run",
        reuse_compile_cache=True,
    )
    populated = run_benchmark(populate_options)
    assert populated["state"] == "completed"
    assert populated["configuration"]["compile_storage_contract"] == "reusable_cache_v2"
    assert not any(case["cache_hit"] for case in populated["cases"])

    second_options = replace(
        populate_options,
        output_path=tmp_path / "cached.json",
        run_id="cached-run",
    )
    second = run_benchmark(second_options)
    assert second["state"] == "completed"
    assert all(case["cache_hit"] for case in second["cases"])
    load_and_validate(tmp_path / "cached.json")


def test_attempt_local_compile_bypasses_cache_publish_eacces_and_cache_mode_fails_fast(
    benchmark_fixture,
    monkeypatch,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    compile_cache_root = (tmp_path / "state" / "cache" / "compile").resolve()
    real_replace = os.replace
    cache_publish_attempts = 0

    def deny_cache_directory_publish(source, destination):
        nonlocal cache_publish_attempts
        source_path = Path(source).resolve()
        destination_path = Path(destination).resolve()
        if (
            source_path.parent == compile_cache_root
            and destination_path.parent == compile_cache_root
            and source_path.is_dir()
        ):
            cache_publish_attempts += 1
            raise PermissionError(errno.EACCES, "simulated cache publication denial")
        return real_replace(source, destination)

    monkeypatch.setattr(cache_module.os, "replace", deny_cache_directory_publish)

    no_cache = run_benchmark(
        replace(
            make_options(
                output_name="attempt-local.json",
                run_id="attempt-local-run",
                compile_repetitions=2,
            ),
            max_workers=4,
            reuse_compile_cache=False,
        )
    )
    assert no_cache["state"] == "completed"
    assert cache_publish_attempts == 0
    assert not compile_cache_root.exists()
    assert all(case["cache_hit"] is False for case in no_cache["cases"])
    for case in no_cache["cases"]:
        attempt_directory = _raw_attempt_directory(
            tmp_path / "state", case["case_id"], case["attempt_index"]
        )
        compile_directory = attempt_directory / "attempt-local-compile"
        assert (compile_directory / "artifact.s").is_file()
        assert (attempt_directory / "binary.elf").is_file()
        assert len(list(compile_directory.glob("repetition-*/artifact.s"))) == 2

    cache_options = replace(
        make_options(
            output_name="cache-publish-denied.json",
            run_id="cache-publish-denied-run",
            compile_repetitions=2,
        ),
        max_workers=1,
        reuse_compile_cache=True,
    )
    with pytest.raises(ExecutionError, match="cannot publish compile cache entry"):
        run_benchmark(cache_options)
    assert cache_publish_attempts == 1
    assert list(compile_cache_root.iterdir()) == []


def test_raw_evidence_verifier_recomputes_a_path_free_terminal_closure(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-verify.json", run_id="raw-verify"),
        max_workers=1,
    )
    completed = run_benchmark(options)

    first = verify_run_raw_evidence(options.output_path, options.state_root)
    second = verify_run_raw_evidence(options.output_path, options.state_root)

    assert first.document == second.document
    assert first.document["schema_version"] == "benchmark-run-raw-evidence.v1"
    assert first.document["run_canonical_sha256"] == sha256_json(completed)
    assert first.document["run_physical_sha256"] == sha256_file(options.output_path)
    assert len(first.document["terminal_journal_sha256"]) == 64
    assert first.document["terminal_journal_event_count"] == 1
    assert first.document["terminal_observed_at"] == completed["completed_at"]
    assert first.document["attempt_count"] == len(completed["cases"])
    assert first.document["terminal_attempt_count"] == len(completed["cases"])
    assert [item["case_id"] for item in first.document["cases"]] == [
        case["case_id"] for case in completed["cases"]
    ]
    assert all(
        item["current_attempt_index"] == 0 and len(item["attempts"]) == 1
        for item in first.document["cases"]
    )
    assert all(path is None for path in first.current_remark_paths.values())
    assert str(tmp_path) not in json.dumps(first.document, sort_keys=True)


def test_read_only_raw_evidence_snapshot_never_enters_or_rewrites_a_lease(
    benchmark_fixture,
    monkeypatch,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-read-only.json", run_id="raw-read-only"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    run_directory = BenchmarkRun(options)._run_directory(completed)
    lock_paths = (
        output_lease_path(options.output_path),
        run_directory / ".run.lock",
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in lock_paths
    }

    def forbid_lease(*_args, **_kwargs):
        raise AssertionError("read-only verification entered an execution lease")

    monkeypatch.setattr(execution_module, "ExclusiveFileLease", forbid_lease)
    snapshot = verify_run_raw_evidence_read_only_snapshot(
        options.output_path, options.state_root
    )

    assert snapshot.verified.document["run_canonical_sha256"] == sha256_json(
        completed
    )
    snapshot.assert_unchanged()
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in lock_paths
    } == before


def test_read_only_raw_evidence_snapshot_rejects_verification_window_drift(
    benchmark_fixture,
    monkeypatch,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-read-only-drift.json", run_id="raw-read-only-drift"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    run_directory = BenchmarkRun(options)._run_directory(completed)
    run_lock = run_directory / ".run.lock"
    original = execution_module._verify_run_raw_evidence_locked

    def verify_then_drift(*args, **kwargs):
        verified = original(*args, **kwargs)
        run_lock.write_bytes(run_lock.read_bytes() + b"\n")
        return verified

    monkeypatch.setattr(
        execution_module, "_verify_run_raw_evidence_locked", verify_then_drift
    )
    with pytest.raises(ExecutionError, match="changed during read-only verification"):
        verify_run_raw_evidence_read_only_snapshot(
            options.output_path, options.state_root
        )


def test_raw_evidence_verifier_rejects_an_internally_valid_normalized_fake(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-normalized-fake.json", run_id="raw-normalized-fake"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    completed["cases"][0]["weight"] *= 2
    validate_document(completed)
    atomic_write_json(options.output_path, completed)

    with pytest.raises(ExecutionError, match="normalized run terminal differs"):
        verify_run_raw_evidence(options.output_path, options.state_root)


def test_raw_evidence_verifier_rejects_normalized_terminal_time_tamper(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-time-fake.json", run_id="raw-time-fake"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    completed["completed_at"] = completed["started_at"]
    completed["updated_at"] = completed["started_at"]
    validate_document(completed)
    atomic_write_json(options.output_path, completed)

    with pytest.raises(ExecutionError, match="normalized run terminal differs"):
        verify_run_raw_evidence(options.output_path, options.state_root)


def test_terminal_run_without_physical_observation_is_not_supplemented(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-old-terminal.json", run_id="raw-old-terminal"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    runner = BenchmarkRun(options)
    run_directory = runner._run_directory(completed)
    terminal_directory = run_directory / "run-terminal"
    saved_terminal = tmp_path / "saved-run-terminal"
    terminal_directory.rename(saved_terminal)

    assert load_and_validate(options.output_path) == completed
    with pytest.raises(ExecutionError, match="lacks a physical run terminal journal"):
        run_benchmark(options)
    assert not terminal_directory.exists()
    with pytest.raises(ExecutionError, match="run terminal journal is missing"):
        verify_run_raw_evidence(options.output_path, options.state_root)


@pytest.mark.parametrize("corruption", ("event", "symlink"))
def test_raw_evidence_verifier_rejects_run_terminal_physical_tamper(
    benchmark_fixture,
    corruption: str,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name=f"run-terminal-{corruption}.json",
            run_id=f"run-terminal-{corruption}",
        ),
        max_workers=1,
    )
    completed = run_benchmark(options)
    run_directory = BenchmarkRun(options)._run_directory(completed)
    terminal_directory = run_directory / "run-terminal"
    if corruption == "event":
        event_path = next(terminal_directory.glob("event-*.json"))
        event_path.write_bytes(event_path.read_bytes() + b"tamper")
        expected = "run terminal journal event content hash differs"
    else:
        physical = tmp_path / "physical-run-terminal"
        terminal_directory.rename(physical)
        terminal_directory.symlink_to(physical, target_is_directory=True)
        expected = "run terminal journal is not a regular directory"

    with pytest.raises(ExecutionError, match=expected):
        verify_run_raw_evidence(options.output_path, options.state_root)


@pytest.mark.parametrize("corruption", ("journal", "raw"))
def test_raw_evidence_verifier_rejects_physical_terminal_tamper(
    benchmark_fixture,
    corruption: str,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name=f"raw-{corruption}-tamper.json",
            run_id=f"raw-{corruption}-tamper",
        ),
        max_workers=1,
    )
    completed = run_benchmark(options)
    case = completed["cases"][0]
    attempt_directory = _raw_attempt_directory(
        options.state_root, case["case_id"], case["attempt_index"]
    )
    if corruption == "journal":
        target = sorted((attempt_directory / "journal").glob("event-*.json"))[-1]
    else:
        target = attempt_directory / "compile-repetition-0000.stdout"
        assert target.is_file()
    target.write_bytes(target.read_bytes() + b"tamper")

    expected = (
        "journal event content hash differs"
        if corruption == "journal"
        else "raw attempt files differ"
    )
    with pytest.raises(ExecutionError, match=expected):
        verify_run_raw_evidence(options.output_path, options.state_root)


def test_raw_evidence_verifier_rejects_lexical_and_physical_symlinks(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-symlink.json", run_id="raw-symlink"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    record_link = tmp_path / "run-link.json"
    record_link.symlink_to(options.output_path)
    state_link = tmp_path / "state-link"
    state_link.symlink_to(options.state_root, target_is_directory=True)

    with pytest.raises(ValidationError, match="must not traverse a symbolic link"):
        verify_run_raw_evidence(record_link, options.state_root)
    with pytest.raises(ValidationError, match="must not traverse a symbolic link"):
        verify_run_raw_evidence(options.output_path, state_link)

    case = completed["cases"][0]
    attempt_directory = _raw_attempt_directory(
        options.state_root, case["case_id"], case["attempt_index"]
    )
    raw_path = attempt_directory / "compile-repetition-0000.stdout"
    physical = raw_path.with_suffix(".physical")
    raw_path.rename(physical)
    raw_path.symlink_to(physical.name)
    with pytest.raises(ExecutionError, match="non-regular entry|symbolic link"):
        verify_run_raw_evidence(options.output_path, options.state_root)


def test_raw_evidence_verifier_accepts_interrupted_unstarted_cases_and_rejects_started_prefix(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    pending_options = replace(
        make_options(output_name="raw-pending.json", run_id="raw-pending"),
        max_workers=1,
    )
    pending_runner = BenchmarkRun(pending_options)
    pending_record = pending_runner._initial_record()
    pending_directory = pending_runner._run_directory(pending_record)
    pending_directory.mkdir(parents=True)
    pending_runner._bind_state_identity(pending_record, pending_directory)
    pending_runner._seal_run_terminal(
        pending_record,
        pending_directory,
        state="interrupted",
    )
    _ensure_execution_leases(pending_options, pending_directory)

    verified = verify_run_raw_evidence(
        pending_options.output_path, pending_options.state_root
    )
    assert verified.document["attempt_count"] == 0
    assert verified.document["terminal_attempt_count"] == 0
    assert all(
        item["current_attempt_index"] is None and item["attempts"] == []
        for item in verified.document["cases"]
    )

    started_options = replace(
        make_options(output_name="raw-started.json", run_id="raw-started"),
        max_workers=1,
    )
    started_runner, _, attempt_directory = _reserve_first_attempt(started_options)
    started_directory = started_runner._run_directory(
        load_and_validate(started_options.output_path)
    )
    _ensure_execution_leases(started_options, started_directory)
    assert {path.name for path in attempt_directory.iterdir()} == {
        "identity.json",
        "journal",
    }
    with pytest.raises(ExecutionError, match="started attempt lacks a durable terminal"):
        verify_run_raw_evidence(started_options.output_path, started_options.state_root)


def test_raw_evidence_verifier_obeys_the_executor_run_lease(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="raw-lease.json", run_id="raw-lease"),
        max_workers=1,
    )
    completed = run_benchmark(options)
    runner = BenchmarkRun(options)
    run_directory = runner._run_directory(completed)
    with ExclusiveFileLease(run_directory / ".run.lock", "execution state", {}):
        with pytest.raises(ExecutionError, match="execution state is already owned"):
            verify_run_raw_evidence(options.output_path, options.state_root)


def test_resume_recovers_durable_terminal_without_reexecution(
    benchmark_fixture,
    monkeypatch,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="resume.json", run_id="resume-run"),
        reuse_compile_cache=True,
    )
    original_seal = BenchmarkRun._seal_run_terminal

    def fail_before_run_terminal(*_args, **_kwargs):
        raise ExecutionError("simulated crash before run terminal seal")

    monkeypatch.setattr(BenchmarkRun, "_seal_run_terminal", fail_before_run_terminal)
    with pytest.raises(ExecutionError, match="simulated crash"):
        run_benchmark(options)
    crashed = load_and_validate(options.output_path)
    assert crashed["state"] == "running"
    terminal_journal_sha256 = crashed["cases"][-1]["attempt_journal_sha256"]

    monkeypatch.setattr(BenchmarkRun, "_seal_run_terminal", original_seal)
    resumed = run_benchmark(options)
    assert resumed["state"] == "completed"
    assert resumed["summary"]["passed_cases"] == 10
    assert resumed["cases"][-1]["cache_hit"] is False
    assert resumed["cases"][-1]["attempt_index"] == 0
    assert resumed["cases"][-1]["attempts"] == []
    assert resumed["cases"][-1]["attempt_journal_sha256"] == terminal_journal_sha256


def test_resume_fails_fast_when_physical_attempt_identity_was_tampered(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="resume-identity.json", run_id="resume-identity-run"),
        reuse_compile_cache=True,
    )
    completed = run_benchmark(options)
    interrupted_case = completed["cases"][-1]

    attempt_directory = _raw_attempt_directory(
        options.state_root,
        interrupted_case["case_id"],
        interrupted_case["attempt_index"],
    )
    identity_path = attempt_directory / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["configuration_sha256"] = "f" * 64
    atomic_write_json(identity_path, identity)

    with pytest.raises(ExecutionError, match="raw attempt identity differs"):
        verify_run_raw_evidence(options.output_path, options.state_root)


def test_resume_retries_only_a_durable_pre_phase_interruption(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="pre-phase-resume.json", run_id="pre-phase-resume"),
        max_workers=1,
    )
    _, reserved, attempt_directory = _reserve_first_attempt(options)
    assert {path.name for path in attempt_directory.iterdir()} == {"identity.json", "journal"}

    resumed = run_benchmark(options)
    first = resumed["cases"][0]
    assert resumed["state"] == "completed"
    assert first["attempt_index"] == 1
    assert len(first["attempts"]) == 1
    interruption = first["attempts"][0]
    assert interruption["status"] == "cancelled"
    assert interruption["cancellation_reason"] == "execution_interrupted"
    assert interruption["attempt_journal_event_count"] == 1
    assert interruption["configuration_sha256"] == reserved["configuration_sha256"]
    assert first["attempt_configuration_sha256"] == reserved["configuration_sha256"]
    evidence = verify_run_raw_evidence(options.output_path, options.state_root)
    assert evidence.document["terminal_journal_event_count"] == 2


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    (
        ("missing_identity", "raw attempt directory collision"),
        ("missing_journal_metadata", "journal metadata is missing"),
        ("orphan_attempt", "physical and normalized attempt sets differ"),
        ("raw_without_journal", "uncommitted raw phase evidence"),
        ("phase_without_terminal", "raw phase evidence lacks a terminal outcome"),
        ("journal_tamper", "journal event content hash differs"),
    ),
)
def test_resume_rejects_unverifiable_physical_crash_windows(
    benchmark_fixture,
    corruption: str,
    expected_error: str,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name=f"crash-{corruption}.json",
            run_id=f"crash-{corruption}",
        ),
        max_workers=1,
    )
    runner, record, attempt_directory = _reserve_first_attempt(options)
    case = record["cases"][0]
    if corruption == "missing_identity":
        (attempt_directory / "identity.json").unlink()
    elif corruption == "missing_journal_metadata":
        (attempt_directory / "journal" / "metadata.json").unlink()
    elif corruption == "orphan_attempt":
        (attempt_directory.parent / "attempt-0001").mkdir()
    elif corruption == "raw_without_journal":
        (attempt_directory / "uncommitted.log").write_bytes(b"uncommitted")
    else:
        assert case["attempt_started_at"] is not None
        assert case["attempt_configuration_sha256"] is not None
        journal = runner._attempt_journal(
            record,
            attempt_directory,
            case_id=case["case_id"],
            attempt_index=case["attempt_index"],
            started_at=case["attempt_started_at"],
            configuration_sha256=case["attempt_configuration_sha256"],
        )
        journal.append_phase_started("compile")
        if corruption == "journal_tamper":
            event_path = next((attempt_directory / "journal").glob("event-*.json"))
            event_path.write_bytes(event_path.read_bytes() + b" ")

    with pytest.raises(ExecutionError, match=expected_error):
        run_benchmark(options)


@pytest.mark.parametrize(
    "committed_stage",
    ("compile", "link", "analyze", "run-0000"),
)
def test_scheduler_seals_committed_phase_prefix_after_worker_infrastructure_failure(
    benchmark_fixture,
    monkeypatch: pytest.MonkeyPatch,
    committed_stage: str,
) -> None:
    _, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name=f"committed-{committed_stage}-infrastructure.json",
        run_id=f"committed-{committed_stage}-infrastructure",
    )
    options = replace(base, max_workers=1, keep_going=False)
    if committed_stage == "link":
        options = replace(
            options,
            linker=StageSpec(
                "external",
                "host",
                (
                    sys.executable,
                    str(tool),
                    "compile",
                    "{artifact}",
                    "{binary}",
                ),
                {},
            ),
        )
    elif committed_stage == "analyze":
        options = replace(
            options,
            analyzer=StageSpec(
                "analyzer",
                "host",
                (
                    sys.executable,
                    str(tool),
                    "analyze-empty",
                    "{binary}",
                    "{analysis_file}",
                ),
                {},
            ),
            analysis_file="analysis/binary.json",
            additional_metrics=(
                MeasurementSpec("elf_text_bytes", "analyzer", "bytes"),
            ),
        )

    original_append = AttemptJournal.append_phase_result
    armed = True

    def inject_after_commit(self, stage, result):
        nonlocal armed
        snapshot = original_append(self, stage, result)
        if armed and stage == committed_stage:
            armed = False
            raise InjectedCommittedPhaseError(f"injected-after-{committed_stage}")
        return snapshot

    monkeypatch.setattr(AttemptJournal, "append_phase_result", inject_after_commit)
    with pytest.raises(
        ExecutionError,
        match=rf"InjectedCommittedPhaseError: injected-after-{committed_stage}",
    ):
        run_benchmark(options)
    monkeypatch.setattr(AttemptJournal, "append_phase_result", original_append)

    interrupted = load_and_validate(options.output_path)
    case = interrupted["cases"][0]
    assert interrupted["state"] == "interrupted"
    assert case["status"] == "cancelled"
    assert case["cancellation_reason"] == "infrastructure_failure"
    assert case["diagnostic"] == (
        f"InjectedCommittedPhaseError: injected-after-{committed_stage}"
    )
    assert case["compile"]["status"] == "ok"
    assert case["artifact_sha256"] is not None
    if committed_stage == "link":
        assert case["link"]["status"] == "ok"
        assert case["binary_sha256"] is not None
    if committed_stage == "analyze":
        assert case["analyze"]["status"] == "ok"
        assert case["analysis_sha256"] is not None
        assert any(
            measurement["metric_id"] == "elf_text_bytes"
            for measurement in case["measurements"]
        )
    if committed_stage == "run-0000":
        assert case["binary_sha256"] is not None
        assert [sample["status"] for sample in case["samples"]] == ["passed"]
    assert case["attempt_journal_sha256"] is not None
    assert case["attempt_journal_event_count"] >= 3

    tampered = deepcopy(interrupted)
    tampered["cases"][0]["compile"]["status"] = "error"
    with pytest.raises(ValidationError, match="infrastructure failure prefix contains"):
        validate_document(tampered)

    with pytest.raises(
        ExecutionError,
        match=rf"cannot resume infrastructure-failed attempt: .*"
        rf"InjectedCommittedPhaseError: injected-after-{committed_stage}",
    ):
        run_benchmark(options)
    resumed = load_and_validate(options.output_path)
    assert resumed["cases"][0]["attempt_index"] == 0
    assert resumed["cases"][0]["attempts"] == []


def test_journal_os_error_identity_reaches_normalized_infrastructure_diagnostic(
    benchmark_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name="journal-os-error-observability.json",
            run_id="journal-os-error-observability",
        ),
        max_workers=1,
        keep_going=False,
    )
    real_open = journal_module.os.open
    injected = False

    def inject_exdev(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        path_name = (
            Path(os.fsdecode(path)).name
            if isinstance(path, (str, bytes, os.PathLike))
            else ""
        )
        if not injected and path_name.startswith(".event-0002-") and flags & os.O_CREAT:
            injected = True
            raise OSError(
                errno.EXDEV,
                "injected cross-device journal create",
                str(tmp_path / "private-raw-evidence"),
            )
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(journal_module.os, "open", inject_exdev)
    with pytest.raises(ExecutionError, match="errno_name=EXDEV"):
        run_benchmark(options)

    assert injected
    interrupted = load_and_validate(options.output_path)
    assert interrupted["state"] == "interrupted"
    case = interrupted["cases"][0]
    assert case["status"] == "cancelled"
    assert case["cancellation_reason"] == "infrastructure_failure"
    assert case["diagnostic"] == (
        "ExecutionError: cannot durably create benchmark evidence: "
        "operation=temporary_create, class=OSError, errno_name=EXDEV, "
        f"errno_code={errno.EXDEV}"
    )
    assert str(tmp_path) not in case["diagnostic"]

    attempt_directory = _raw_attempt_directory(
        options.state_root,
        case["case_id"],
        case["attempt_index"],
    )
    metadata = json.loads(
        (attempt_directory / "journal" / "metadata.json").read_text(encoding="utf-8")
    )
    snapshot = AttemptJournal(
        attempt_directory,
        identity_sha256=metadata["identity_sha256"],
    ).load()
    assert snapshot.terminal_case_result is not None
    assert snapshot.terminal_case_result["diagnostic"] == case["diagnostic"]


def test_scheduler_preserves_committed_runtime_failure_after_worker_exception(
    benchmark_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name="committed-runtime-failure.json",
            run_id="committed-runtime-failure",
            behavior="return-one",
        ),
        max_workers=1,
        keep_going=False,
    )
    original_append = AttemptJournal.append_phase_result
    armed = True

    def inject_after_committed_failure(self, stage, result):
        nonlocal armed
        snapshot = original_append(self, stage, result)
        if armed and stage == "run-0000":
            armed = False
            raise InjectedCommittedPhaseError("injected-after-wrong-output")
        return snapshot

    monkeypatch.setattr(
        AttemptJournal,
        "append_phase_result",
        inject_after_committed_failure,
    )
    with pytest.raises(
        ExecutionError,
        match="InjectedCommittedPhaseError: injected-after-wrong-output",
    ):
        run_benchmark(options)
    monkeypatch.setattr(AttemptJournal, "append_phase_result", original_append)

    interrupted = load_and_validate(options.output_path)
    case = interrupted["cases"][0]
    assert interrupted["state"] == "interrupted"
    assert case["status"] == "wrong_output"
    assert case["cancellation_reason"] is None
    assert case["samples"][-1]["status"] == "wrong_output"
    assert case["diagnostic"] == case["samples"][-1]["diagnostic"]
    assert case["attempt_journal_sha256"] is not None
    assert case["attempt_journal_event_count"] >= 4
    assert case["attempt_index"] == 0
    assert case["attempts"] == []


@pytest.mark.parametrize("publish_before_error", (False, True))
def test_scheduler_terminal_publish_failure_keeps_normalized_record_transactional(
    benchmark_fixture,
    monkeypatch: pytest.MonkeyPatch,
    publish_before_error: bool,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name=f"worker-and-terminal-publish-failure-{publish_before_error}.json",
            run_id=f"worker-and-terminal-publish-failure-{publish_before_error}",
        ),
        max_workers=1,
        keep_going=False,
    )
    original_phase_result = AttemptJournal.append_phase_result
    original_terminal = AttemptJournal.append_terminal
    phase_armed = True

    def fail_worker_after_phase_commit(self, stage, result):
        nonlocal phase_armed
        snapshot = original_phase_result(self, stage, result)
        if phase_armed and stage == "compile":
            phase_armed = False
            raise InjectedCommittedPhaseError("worker-failed-after-compile-commit")
        return snapshot

    def fail_terminal_publish(self, case_result):
        if publish_before_error:
            original_terminal(self, case_result)
        raise InjectedTerminalPublishError("terminal-create-failed")

    monkeypatch.setattr(
        AttemptJournal,
        "append_phase_result",
        fail_worker_after_phase_commit,
    )
    monkeypatch.setattr(
        AttemptJournal,
        "append_terminal",
        fail_terminal_publish,
    )

    with pytest.raises(ExecutionError) as raised:
        run_benchmark(options)
    assert "InjectedCommittedPhaseError: worker-failed-after-compile-commit" in str(
        raised.value
    )
    assert "InjectedTerminalPublishError: terminal-create-failed" in str(raised.value)
    cause = raised.value.__cause__
    assert isinstance(cause, BaseExceptionGroup)
    assert [type(error) for error in cause.exceptions] == [
        InjectedCommittedPhaseError,
        InjectedTerminalPublishError,
    ]

    interrupted = load_and_validate(options.output_path)
    assert interrupted["state"] == "interrupted"
    case = interrupted["cases"][0]
    assert case["status"] == "pending"
    assert case["cancellation_reason"] is None
    assert case["attempt_journal_sha256"] is None
    assert case["attempt_journal_event_count"] is None
    assert case["compile"] is None
    assert case["compile_samples"] == []

    attempt_directory = _raw_attempt_directory(
        options.state_root,
        case["case_id"],
        case["attempt_index"],
    )
    metadata = json.loads(
        (attempt_directory / "journal" / "metadata.json").read_text(encoding="utf-8")
    )
    journal = AttemptJournal(
        attempt_directory,
        identity_sha256=metadata["identity_sha256"],
    )
    snapshot = journal.load()
    assert snapshot.has_phase_evidence
    assert snapshot.latest_committed_case_prefix is not None
    assert (snapshot.terminal_case_result is not None) is publish_before_error

    monkeypatch.setattr(AttemptJournal, "append_phase_result", original_phase_result)
    monkeypatch.setattr(AttemptJournal, "append_terminal", original_terminal)
    resume_error = (
        "cannot resume infrastructure-failed attempt"
        if publish_before_error
        else "raw phase evidence lacks a terminal outcome"
    )
    with pytest.raises(ExecutionError, match=resume_error):
        run_benchmark(options)
    unchanged = load_and_validate(options.output_path)
    assert unchanged["state"] == "interrupted"
    assert unchanged["cases"][0] == case


def test_resume_rejects_success_metric_masquerading_as_the_durable_attempt(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = make_options(output_name="metric-masquerade.json", run_id="metric-masquerade")
    completed = run_benchmark(options)
    case = completed["cases"][0]
    assert case["status"] == "passed"
    case["samples"][0]["measurements"][0]["value"] += 1
    atomic_write_json(options.output_path, completed)

    with pytest.raises(ExecutionError, match="normalized run terminal differs"):
        run_benchmark(options)


def test_resume_restores_wrong_output_without_retrying_the_real_failure(
    benchmark_fixture,
    monkeypatch,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = make_options(
        output_name="wrong-output-recovery.json",
        run_id="wrong-output-recovery",
        behavior="return-one",
    )
    original_seal = BenchmarkRun._seal_run_terminal

    def fail_before_run_terminal(*_args, **_kwargs):
        raise ExecutionError("simulated crash before failed run terminal seal")

    monkeypatch.setattr(BenchmarkRun, "_seal_run_terminal", fail_before_run_terminal)
    with pytest.raises(ExecutionError, match="simulated crash"):
        run_benchmark(options)
    crashed = load_and_validate(options.output_path)
    assert crashed["state"] == "running"
    recovered_case = crashed["cases"][-1]
    terminal_journal_sha256 = recovered_case["attempt_journal_sha256"]
    assert recovered_case["status"] == "wrong_output"

    monkeypatch.setattr(BenchmarkRun, "_seal_run_terminal", original_seal)
    resumed = run_benchmark(options)
    restored = resumed["cases"][-1]
    assert resumed["state"] == "failed"
    assert restored["status"] == "wrong_output"
    assert restored["attempt_index"] == 0
    assert restored["attempts"] == []
    assert restored["attempt_journal_sha256"] == terminal_journal_sha256
    failed_evidence = verify_run_raw_evidence(options.output_path, options.state_root)
    assert failed_evidence.document["attempt_count"] == len(resumed["cases"])
    assert failed_evidence.document["terminal_attempt_count"] == len(resumed["cases"])


@pytest.mark.parametrize(("initial_retry", "reopen_retry"), ((False, True), (True, False)))
def test_retry_policy_cannot_rebind_an_existing_run(
    benchmark_fixture,
    initial_retry: bool,
    reopen_retry: bool,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name=f"retry-binding-{initial_retry}.json",
            run_id=f"retry-binding-{initial_retry}",
        ),
        retry_failures=initial_retry,
    )
    completed = run_benchmark(options)
    original_configuration_sha256 = completed["configuration_sha256"]

    with pytest.raises(ConfigurationError, match="configuration digest changed"):
        run_benchmark(replace(options, retry_failures=reopen_retry))

    unchanged = run_benchmark(options)
    assert unchanged["configuration_sha256"] == original_configuration_sha256
    assert unchanged["configuration"]["retry_failures"] is initial_retry


def test_failed_run_is_terminal_even_when_retry_failures_was_configured(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, tool, make_options = benchmark_fixture
    marker = tmp_path / "compile-failed-once.marker"
    base = make_options(
        output_name="raw-retry.json",
        run_id="raw-retry-run",
        compile_repetitions=1,
    )
    options = replace(
        base,
        compiler=replace(
            base.compiler,
            command=(
                sys.executable,
                str(tool),
                "compile-fail-once",
                "{source}",
                "{artifact}",
                str(marker),
            ),
        ),
        max_workers=1,
        keep_going=False,
        retry_failures=True,
    )

    first = run_benchmark(options)
    assert first["state"] == "failed"
    failed = first["cases"][0]
    assert failed["status"] == "compile_error"
    first_directory = _raw_attempt_directory(options.state_root, failed["case_id"], 0)
    first_stderr = first_directory / "compile-repetition-0000.stderr"
    first_payload = first_stderr.read_bytes()
    assert first_payload.decode().splitlines() == ["first-attempt-failure"]
    assert sha256_file(first_stderr) == failed["compile_samples"][0]["stderr"]["sha256"]
    first_workspace_stderr = (
        first_directory
        / "attempt-local-compile"
        / "repetition-0000"
        / "compile.stderr"
    )
    assert first_workspace_stderr.read_bytes() == first_payload

    reopened = run_benchmark(options)
    assert reopened == first
    current = reopened["cases"][0]
    assert current["attempt_index"] == 0
    assert current["attempts"] == []
    assert first_stderr.read_bytes() == first_payload
    assert sha256_file(first_stderr) == current["compile_samples"][0]["stderr"]["sha256"]
    assert not (first_directory.parent / "attempt-0001").exists()
    assert first_workspace_stderr.read_bytes() == first_payload
    retry_evidence = verify_run_raw_evidence(options.output_path, options.state_root)
    assert retry_evidence.document["attempt_count"] == 1
    assert retry_evidence.document["terminal_attempt_count"] == 1
    assert [
        attempt["attempt_index"]
        for attempt in retry_evidence.document["cases"][0]["attempts"]
    ] == [0]

    non_contiguous = deepcopy(reopened)
    non_contiguous["cases"][0]["attempt_index"] = 1
    with pytest.raises(ValidationError, match="current attempt index is not contiguous"):
        validate_document(non_contiguous)
    unbound = deepcopy(reopened)
    unbound["cases"][0]["attempt_started_at"] = None
    with pytest.raises(ValidationError, match="start/configuration binding is inconsistent"):
        validate_document(unbound)


def test_cache_v2_recomputes_cold_sample_remark_event_count(benchmark_fixture) -> None:
    tmp_path, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name="cache-count-first.json",
        run_id="cache-count-first",
        compile_repetitions=1,
    )
    options = replace(
        base,
        compiler=replace(
            base.compiler,
            command=(
                sys.executable,
                str(tool),
                "compile-remarks",
                "{source}",
                "{artifact}",
                "{compile_sample_index}",
                "{remarks_file}",
            ),
        ),
        remarks_file="remarks.jsonl",
        reuse_compile_cache=True,
        max_workers=1,
    )
    first = run_benchmark(options)
    assert first["state"] == "completed"
    observed_remarks: list[tuple[str, Path]] = []
    verified = verify_run_raw_evidence(
        options.output_path,
        options.state_root,
        remark_validator=lambda path, case: observed_remarks.append(
            (case["case_id"], path)
        ),
    )
    assert len(observed_remarks) == len(first["cases"])
    assert all(path.is_file() for _, path in observed_remarks)
    assert all(path is not None for path in verified.current_remark_paths.values())
    assert all(
        item["attempts"][-1]["remark_files_sha256"] is not None
        for item in verified.document["cases"]
    )

    metadata_path = next((options.state_root / "cache" / "compile").glob("*/metadata.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["version"] == 2
    assert metadata["samples"][0]["remarks_event_count"] == 1
    metadata["samples"][0]["remarks_event_count"] = 2
    atomic_write_json(metadata_path, metadata)

    second = replace(
        options,
        output_path=tmp_path / "cache-count-second.json",
        run_id="cache-count-second",
    )
    with pytest.raises(ExecutionError, match="event-count integrity verification"):
        run_benchmark(second)


def test_reusable_cache_v2_rejects_missing_or_tampered_compile_streams(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(
            output_name="cache-stream-source.json",
            run_id="cache-stream-source",
            compile_repetitions=1,
        ),
        reuse_compile_cache=True,
        max_workers=1,
    )
    source_run = run_benchmark(options)
    assert source_run["state"] == "completed"
    entry_directories = sorted(
        metadata_path.parent
        for metadata_path in (options.state_root / "cache" / "compile").glob(
            "*/metadata.json"
        )
    )
    assert len(entry_directories) == len(source_run["cases"])
    for directory in entry_directories:
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        for stream_name in ("stdout", "stderr"):
            assert set(metadata["phase"][stream_name]) == {"sha256", "size_bytes"}
            assert set(metadata["samples"][0][stream_name]) == {"sha256", "size_bytes"}

    mutations = (
        (Path("repetition-0000/compile.stdout"), False),
        (Path("repetition-0000/compile.stderr"), False),
        (Path("compile.stdout"), False),
        (Path("compile.stderr"), False),
        (Path("repetition-0000/compile.stdout"), True),
    )
    for index, (relative_path, remove) in enumerate(mutations):
        originals: dict[Path, bytes] = {}
        for directory in entry_directories:
            target = directory / relative_path
            originals[target] = target.read_bytes()
            if remove:
                target.unlink()
            else:
                target.write_bytes(originals[target] + b"tampered-cache-stream\n")
        try:
            changed = replace(
                options,
                output_path=tmp_path / f"cache-stream-tampered-{index}.json",
                run_id=f"cache-stream-tampered-{index}",
            )
            expected = "is missing" if remove else "integrity verification"
            with pytest.raises(ExecutionError, match=expected):
                run_benchmark(changed)
        finally:
            for target, payload in originals.items():
                target.write_bytes(payload)


def test_exact_output_rejects_appended_newline(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    record = run_benchmark(make_options(output_name="wrong.json", behavior="append", run_id="wrong-run"))
    assert record["state"] == "failed"
    assert all(case["status"] == "wrong_output" for case in record["cases"])
    assert all(case["samples"][0]["first_mismatch_offset"] is not None for case in record["cases"])


def test_timeout_is_right_censored(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = make_options(
        output_name="timeout.json",
        behavior="sleep",
        run_timeout=0.05,
        run_id="timeout-run",
    )
    options = replace(options, max_workers=1, keep_going=False)
    record = run_benchmark(options)
    timed_out = next(case for case in record["cases"] if case["status"] == "timeout")
    sample = timed_out["samples"][0]
    assert sample["censoring"] == "right"
    assert sample["censor_metric_id"] == "wall_time_ns"
    assert sample["censor_bound"] == 50_000_000
    assert record["summary"]["censored_cases"] == 1
    assert record["summary"]["pending_cases"] == 0
    assert record["summary"]["failed_cases"] == 10
    assert [case["status"] for case in record["cases"]].count("timeout") == 1
    assert [case["status"] for case in record["cases"]].count("cancelled") == 9
    assert not any(
        case["status"] in {"compile_error", "link_error", "analyze_error", "runtime_error"}
        for case in record["cases"]
    )
    load_and_validate(options.output_path)


def test_qemu_metric_file_is_cleared_and_parsed_without_shell(benchmark_fixture) -> None:
    _, _, _, tool, make_options = benchmark_fixture
    options = make_options(
        output_name="metric-file.json",
        primary_metric_id="dynamic_instruction_count",
        metric_source="file",
        metric_pattern=r"instructions=(?P<value>\d+)",
        metric_unit="instructions",
        run_id="metric-file-run",
        additional_metrics=(
            MeasurementSpec("dynamic_load_count", "file", "instructions", r"loads=(?P<value>\d+)"),
            MeasurementSpec("dynamic_store_count", "file", "instructions", r"stores=(?P<value>\d+)"),
            MeasurementSpec("l1d_miss_count", "file", "misses", r"l1d=(?P<value>\d+)"),
        ),
    )
    runner = StageSpec(
        "qemu",
        "host",
        (
            sys.executable, "{runner_executable}", "run", "{binary}", "file",
            "{metric_file}", "{qemu_binary}", "{profile_plugin_binary}", "{input}",
            "{cache_plugin_binary}",
        ),
        {},
    )
    options = _rebind_protocol(replace(
        options,
        metric_file="metrics/plugin.log",
    ), runner=runner, name="metric-file-protocol")
    record = run_benchmark(options)
    assert record["state"] == "completed"
    for case in record["cases"]:
        for sample in case["samples"]:
            measurements = {item["metric_id"]: item["value"] for item in sample["measurements"]}
            assert measurements == {
                "dynamic_instruction_count": 80,
                "dynamic_load_count": 30,
                "dynamic_store_count": 12,
                "l1d_miss_count": 4,
            }


def test_record_never_contains_absolute_paths(benchmark_fixture) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    record = run_benchmark(make_options(output_name="private.json", run_id="private-run"))
    payload = json.dumps(record, ensure_ascii=False)
    assert str(tmp_path) not in payload
    assert str(tmp_path).replace("\\", "/") not in payload


def test_triple_consistency_rejects_deterministic_counter_drift(benchmark_fixture) -> None:
    _, _, _, tool, make_options = benchmark_fixture
    options = make_options(output_name="inconsistent.json", run_id="inconsistent-run")
    runner = StageSpec(
        "qemu",
        "host",
        (
            sys.executable, "{runner_executable}", "run", "{binary}", "vary",
            "{sample_index}", "{qemu_binary}", "{profile_plugin_binary}", "{input}",
            "{cache_plugin_binary}",
            "{metric_file}",
        ),
        {},
    )
    options = _rebind_protocol(options, runner=runner, name="vary-protocol")
    record = run_benchmark(options)
    selected = next(case for case in record["cases"] if case["consistency_selected"])
    assert selected["status"] == "measurement_inconsistent"
    assert selected["consistency_passed"] is False
    assert selected["consistency_mismatched_metrics"] == ["dynamic_instruction_count"]
    assert record["state"] == "failed"


def test_post_link_analyzer_records_sections_static_classes_and_unavailable_metrics(
    benchmark_fixture,
) -> None:
    _, _, _, tool, make_options = benchmark_fixture
    analyzer_metrics = tuple(
        MeasurementSpec(metric_id, "analyzer", unit)
        for metric_id, unit in (
            ("elf_text_bytes", "bytes"),
            ("elf_rodata_bytes", "bytes"),
            ("elf_data_bytes", "bytes"),
            ("static_integer_instructions", "instructions"),
            ("static_branch_instructions", "instructions"),
            ("static_load_instructions", "instructions"),
            ("static_store_instructions", "instructions"),
            ("spill_count", "instructions"),
            ("reload_count", "instructions"),
            ("stack_frame_bytes", "bytes"),
        )
    )
    options = make_options(
        output_name="analyzed.json",
        run_id="analyzed-run",
        additional_metrics=analyzer_metrics,
    )
    options = replace(
        options,
        analyzer=StageSpec(
            "analyzer",
            "host",
            (sys.executable, str(tool), "analyze", "{binary}", "{analysis_file}"),
            {},
        ),
        analysis_file="analysis/binary.json",
    )
    record = run_benchmark(options)
    assert record["state"] == "completed"
    for case in record["cases"]:
        measurements = {item["metric_id"]: item for item in case["measurements"]}
        assert measurements["elf_text_bytes"]["value"] == 101
        assert measurements["stack_frame_bytes"]["value"] == 32
        assert measurements["spill_count"] == {
            "metric_id": "spill_count",
            "value": None,
            "unit": "instructions",
            "origin": "observed",
            "availability": "unavailable",
            "reason": "not_supported_by_toolchain",
        }


def test_mismatched_toolchain_cannot_be_labeled_official(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    base = make_options(output_name="invalid-official.json")
    options = replace(
        base,
        runner=replace(base.runner, kind="boom"),
        environment_label="official",
        evidence_level="boom_hardware",
        measurement_protocol_path=None,
        measurement_protocol_assets=(),
        provenance=replace(
            base.provenance,
            measurement_protocol_id=None,
            measurement_protocol_sha256=None,
        ),
    )
    with pytest.raises(ConfigurationError, match="exact expected version"):
        run_benchmark(options)


def test_compiler_artifact_hash_invalidates_compile_cache(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    first_options = replace(
        make_options(output_name="compiler-a.json", run_id="compiler-a"),
        reuse_compile_cache=True,
    )
    first = run_benchmark(first_options)
    assert not any(case["cache_hit"] for case in first["cases"])
    changed_provenance = replace(
        first_options.provenance,
        compiler_artifact_sha256="f" * 64,
    )
    second_options = replace(
        first_options,
        output_path=first_options.output_path.with_name("compiler-b.json"),
        run_id="compiler-b",
        provenance=changed_provenance,
    )
    second = run_benchmark(second_options)
    assert not any(case["cache_hit"] for case in second["cases"])

    changed_source = first_options.workspace_root / "changed-profile-plugin.c"
    changed_source.write_bytes(b"changed protocol source\n")
    changed_assets = dict(first_options.measurement_protocol_assets)
    changed_assets["profile_plugin_source"] = changed_source
    protocol_options = _rebind_protocol(replace(
        first_options,
        output_path=first_options.output_path.with_name("protocol-changed.json"),
        run_id="protocol-changed",
    ), assets=changed_assets, name="changed-protocol")
    protocol_run = run_benchmark(protocol_options)
    assert not any(case["cache_hit"] for case in protocol_run["cases"])


def test_qemu_proxy_requires_exact_measurement_protocol_snapshot(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    base = make_options(output_name="protocol-required.json")
    missing = replace(
        base,
        measurement_protocol_path=None,
        provenance=replace(
            base.provenance,
            measurement_protocol_id=None,
            measurement_protocol_sha256=None,
        ),
    )
    with pytest.raises(ConfigurationError, match="measurement-protocol"):
        run_benchmark(missing)
    drifted = replace(
        base,
        provenance=replace(base.provenance, measurement_protocol_sha256="f" * 64),
    )
    with pytest.raises(ConfigurationError, match="does not match provenance"):
        run_benchmark(drifted)
    changed_asset = base.workspace_root / "drifted-runtime.c"
    changed_asset.write_bytes(b"drifted runtime bytes\n")
    drifted_assets = dict(base.measurement_protocol_assets)
    drifted_assets["runtime_source"] = changed_asset
    physical_drift = replace(
        base,
        measurement_protocol_assets=tuple(drifted_assets.items()),
    )
    with pytest.raises(ValidationError, match="source hash drift"):
        run_benchmark(physical_drift)


def test_relative_protocol_assets_use_one_workspace_mapping_for_verify_and_execute(
    benchmark_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name="relative-protocol-assets.json",
        run_id="relative-protocol-assets",
    )
    relative_assets: list[tuple[str, Path]] = []
    for key, declared in base.measurement_protocol_assets:
        try:
            relative = declared.relative_to(workspace_root)
        except ValueError:
            relative = declared
        relative_assets.append((key, relative))
    assert dict(relative_assets)["runner_executable"] == tool.relative_to(workspace_root)

    options = replace(
        base,
        measurement_protocol_assets=tuple(relative_assets),
        max_workers=1,
    )
    monkeypatch.chdir(workspace_root.parent)
    assert not Path.cwd().is_relative_to(workspace_root)

    runner = BenchmarkRun(options)
    expected_runner = tool.resolve(strict=True)
    assert runner.measurement_protocol_assets["runner_executable"] == expected_runner
    assert runner.measurement_protocol_assets["profile_plugin_source"] == expected_runner

    record = runner.execute()
    assert record["state"] == "completed"
    assert all(case["status"] == "passed" for case in record["cases"])


def test_cold_compile_logs_survive_mid_repetition_failure(benchmark_fixture) -> None:
    tmp_path, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name="compile-failure.json", run_id="compile-failure", compile_repetitions=5,
    )
    options = replace(
        base,
        compiler=replace(
            base.compiler,
            command=(
                sys.executable, str(tool), "compile-sampled", "{source}", "{artifact}",
                "{compile_sample_index}",
            ),
        ),
        max_workers=1,
    )
    record = run_benchmark(options)
    assert record["state"] == "failed"
    case_directories = list(
        (tmp_path / "state" / "runs").glob("*/cases/*/attempts/attempt-0000")
    )
    assert case_directories
    for case_directory in case_directories:
        assert (case_directory / "compile-repetition-0000.stderr").is_file()
        assert (case_directory / "compile-repetition-0001.stderr").is_file()
        assert (case_directory / "compile-repetition-0002.stderr").read_text(encoding="utf-8") == "compile-sample=2\n"
        assert not (case_directory / "compile-repetition-0003.stderr").exists()


def test_cold_compile_nondeterminism_preserves_structured_artifact_evidence(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name="compile-nondeterminism.json",
        run_id="compile-nondeterminism",
        compile_repetitions=5,
    )
    options = replace(
        base,
        compiler=replace(
            base.compiler,
            command=(
                sys.executable,
                str(tool),
                "compile-vary",
                "{source}",
                "{artifact}",
                "{compile_sample_index}",
            ),
        ),
        max_workers=1,
        keep_going=False,
    )

    record = run_benchmark(options)
    failed = record["cases"][0]
    assert failed["status"] == "compile_error"
    assert len(failed["compile_samples"]) == 2
    first, current = failed["compile_samples"]
    assert first["artifact_sha256"] != current["artifact_sha256"]
    assert first["artifact_size_bytes"] > 0
    assert current["artifact_size_bytes"] > 0
    assert first["remarks_sha256"] is None
    assert first["remarks_event_count"] is None
    assert failed["diagnostic"] == (
        "compiler artifact differs across cold repetitions: "
        "sample_index=1, first_sample_index=0, "
        f"first_sha256={first['artifact_sha256']}, current_sha256={current['artifact_sha256']}"
    )
    assert str(tmp_path) not in failed["diagnostic"]
    case_directory = next(
        (tmp_path / "state" / "runs").glob("*/cases/*/attempts/attempt-0000")
    )
    for index, sample in enumerate((first, current)):
        raw_artifact = case_directory / f"compile-repetition-{index:04d}.artifact.s"
        assert raw_artifact.is_file()
        assert raw_artifact.stat().st_size == sample["artifact_size_bytes"]
    load_and_validate(options.output_path)


def test_each_cold_compile_sample_records_and_preserves_remarks_evidence(
    benchmark_fixture,
) -> None:
    tmp_path, _, _, tool, make_options = benchmark_fixture
    base = make_options(
        output_name="compile-remarks.json",
        run_id="compile-remarks",
        compile_repetitions=3,
    )
    options = replace(
        base,
        compiler=replace(
            base.compiler,
            command=(
                sys.executable,
                str(tool),
                "compile-remarks",
                "{source}",
                "{artifact}",
                "{compile_sample_index}",
                "{remarks_file}",
            ),
        ),
        remarks_file="remarks.jsonl",
        max_workers=1,
    )

    record = run_benchmark(options)
    assert record["state"] == "completed"
    for case in record["cases"]:
        assert len(case["compile_samples"]) == 3
        assert {sample["remarks_event_count"] for sample in case["compile_samples"]} == {1}
        assert len({sample["remarks_sha256"] for sample in case["compile_samples"]}) == 3
        assert all(sample["artifact_size_bytes"] > 0 for sample in case["compile_samples"])
        assert case["remarks_sha256"] == case["compile_samples"][-1]["remarks_sha256"]
    case_directory = next(
        (tmp_path / "state" / "runs").glob("*/cases/*/attempts/attempt-0000")
    )
    for index in range(3):
        assert (case_directory / f"compile-repetition-{index:04d}.artifact.s").is_file()
        assert (case_directory / f"compile-repetition-{index:04d}.remarks.jsonl").is_file()
    compile_directory = case_directory / "attempt-local-compile"
    assert (compile_directory / "artifact.s").is_file()
    assert (compile_directory / "remarks.jsonl").is_file()
    for index in range(3):
        repetition_directory = compile_directory / f"repetition-{index:04d}"
        assert (repetition_directory / "artifact.s").is_file()
        assert (repetition_directory / "remarks.jsonl").is_file()
        assert (repetition_directory / "compile.stdout").is_file()
        assert (repetition_directory / "compile.stderr").is_file()

    tampered = deepcopy(record)
    tampered["cases"][0]["compile_samples"][0]["artifact_size_bytes"] = None
    with pytest.raises(ValidationError, match="artifact hash/size evidence is inconsistent"):
        validate_document(tampered)


def test_output_contracts_compare_stdout_and_main_return_independently(benchmark_fixture) -> None:
    _, _, _, tool, make_options = benchmark_fixture
    process_exit = make_options(output_name="process-exit.json", behavior="process-exit", run_id="process-exit")
    process_exit = replace(process_exit, output_contract="process_exit", max_workers=1)
    assert run_benchmark(process_exit)["state"] == "completed"

    result_file = make_options(output_name="result-file.json", behavior="result-file", run_id="result-file")
    result_file = replace(
        result_file,
        runner=replace(
            result_file.runner,
            command=(
                sys.executable, "{runner_executable}", "run", "{binary}",
                "result-file", "{result_file}", "{qemu_binary}", "{input}",
                "{profile_plugin_binary}", "{cache_plugin_binary}", "{metric_file}",
            ),
        ),
        output_contract="result_file",
        result_file="return.txt",
        max_workers=1,
    )
    result_file = _rebind_protocol(result_file, name="result-file-protocol")
    assert run_benchmark(result_file)["state"] == "completed"

    wrong_return = make_options(output_name="wrong-return.json", behavior="return-one", run_id="wrong-return")
    wrong = run_benchmark(replace(wrong_return, max_workers=1, keep_going=False))
    sample = wrong["cases"][0]["samples"][0]
    assert wrong["cases"][0]["status"] == "wrong_output"
    assert sample["first_mismatch_offset"] is None
    assert sample["expected_return_uint8"] == 0
    assert sample["observed_return_uint8"] == 1


def test_baseline_derived_timeout_uses_each_case_median(benchmark_fixture) -> None:
    _, _, _, _, make_options = benchmark_fixture
    baseline_options = replace(
        make_options(output_name="timeout-baseline.json", run_id="timeout-baseline"),
        timeout_policy="initial",
        run_timeout_seconds=1800.0,
        max_workers=1,
    )
    baseline = run_benchmark(baseline_options)
    for sample in baseline["cases"][0]["samples"]:
        sample["duration_ns"] = 50_000_000_000
    for sample in baseline["cases"][1]["samples"]:
        sample["duration_ns"] = 700_000_000_000
    atomic_write_json(baseline_options.output_path, validate_document(baseline))

    derived_options = replace(
        make_options(output_name="timeout-derived.json", run_id="timeout-derived"),
        timeout_policy="baseline_derived",
        baseline_timeout_path=baseline_options.output_path,
        run_timeout_seconds=1800.0,
        max_workers=1,
    )
    derived = run_benchmark(derived_options)
    assert derived["cases"][0]["effective_timeout_seconds"] == 150.0
    assert derived["cases"][1]["effective_timeout_seconds"] == 1800.0
