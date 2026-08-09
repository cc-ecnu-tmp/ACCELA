from __future__ import annotations

import json
import multiprocessing
import sys
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from tools.benchmark.execution import MeasurementSpec, _summary, run_benchmark
from tools.benchmark.errors import ConfigurationError, ExecutionError, ValidationError
from tools.benchmark.adapters import StageSpec
from tools.benchmark.schema import load_and_validate, validate_document
from tools.benchmark.protocol import capture_measurement_protocol
from tools.benchmark.process import run_process
from tools.benchmark.util import atomic_write_json, sha256_file, sha256_json


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


def _rebind_protocol(options, *, runner=None, assets=None, name: str):
    bound_runner = runner or options.runner
    bound_assets = dict(options.measurement_protocol_assets) if assets is None else dict(assets)
    previous = load_and_validate(options.measurement_protocol_path)
    protocol = capture_measurement_protocol(
        protocol_id=previous["protocol_id"],
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

    second_options = replace(
        make_options(
            output_name="cached.json",
            additional_metrics=metrics,
            compile_repetitions=5,
        ),
        run_id="cached-run",
        reuse_compile_cache=True,
    )
    second = run_benchmark(second_options)
    assert second["state"] == "completed"
    assert all(case["cache_hit"] for case in second["cases"])
    load_and_validate(tmp_path / "cached.json")


def test_resume_only_reexecutes_cancelled_case(benchmark_fixture) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="resume.json", run_id="resume-run"),
        reuse_compile_cache=True,
    )
    completed = run_benchmark(options)
    resumed_case = completed["cases"][-1]
    resumed_case.update(
        status="cancelled",
        cache_hit=False,
        compile=None,
        link=None,
        measurements=[],
        samples=[],
        consistency_passed=(False if resumed_case["consistency_selected"] else None),
        diagnostic="simulated interruption",
    )
    completed["state"] = "failed"
    completed["completed_at"] = completed["updated_at"]
    completed["summary"] = _summary(completed["cases"])
    atomic_write_json(tmp_path / "resume.json", completed)
    resumed = run_benchmark(options)
    assert resumed["state"] == "completed"
    assert resumed["summary"]["passed_cases"] == 10
    assert resumed["cases"][-1]["cache_hit"] is True
    assert resumed["cases"][-1]["attempts"][0]["status"] == "cancelled"


def test_retry_failures_preserves_prior_attempt_evidence(benchmark_fixture) -> None:
    tmp_path, _, _, _, make_options = benchmark_fixture
    options = replace(
        make_options(output_name="retry.json", run_id="retry-run"),
        reuse_compile_cache=True,
    )
    completed = run_benchmark(options)
    failed_case = completed["cases"][0]
    failed_case.update(status="wrong_output", diagnostic="retained first-attempt failure")
    completed["state"] = "failed"
    completed["completed_at"] = completed["updated_at"]
    completed["summary"] = _summary(completed["cases"])
    atomic_write_json(tmp_path / "retry.json", completed)

    retried = run_benchmark(replace(options, retry_failures=True))
    assert retried["state"] == "completed"
    assert retried["configuration"]["retry_failures"] is True
    attempt = retried["cases"][0]["attempts"][0]
    assert attempt["status"] == "wrong_output"
    assert attempt["failure_summary"] == "correctness_mismatch"
    assert attempt["configuration_sha256"] != retried["configuration_sha256"]
    assert attempt["diagnostic"] == "retained first-attempt failure"
    assert attempt["samples"]


def test_retry_uses_new_attempt_directory_and_preserves_failed_raw_evidence(
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

    retried = run_benchmark(replace(options, retry_failures=True))
    assert retried["state"] == "completed"
    current = retried["cases"][0]
    assert current["attempt_index"] == 1
    assert len(current["attempts"]) == 1
    archived = current["attempts"][0]
    assert archived["attempt_index"] == 0
    assert archived["status"] == "compile_error"
    assert archived["started_at"] == failed["attempt_started_at"]
    assert first_stderr.read_bytes() == first_payload
    assert sha256_file(first_stderr) == archived["compile_samples"][0]["stderr"]["sha256"]

    second_directory = _raw_attempt_directory(options.state_root, failed["case_id"], 1)
    assert second_directory != first_directory
    assert (
        second_directory / "compile-repetition-0000.stderr"
    ).read_text(encoding="utf-8").splitlines() == ["later-attempt-success"]
    assert archived["configuration_sha256"] != current["attempt_configuration_sha256"]
    assert all(
        case["attempt_index"] == 0 and not case["attempts"]
        for case in retried["cases"][1:]
    )

    non_contiguous = deepcopy(retried)
    non_contiguous["cases"][0]["attempt_index"] = 0
    with pytest.raises(ValidationError, match="current attempt index is not contiguous"):
        validate_document(non_contiguous)
    unbound = deepcopy(retried)
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
