from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from tools.benchmark.ablation import _require_formal_measurement
from tools.benchmark.errors import ConfigurationError, ValidationError
from tools.benchmark.execution import run_benchmark
from tools.benchmark.schema import validate_document
from tools.benchmark.stats import compare_runs
from tools.benchmark.tests.test_stats_ablation_report import make_run
from tools.benchmark.util import raw_attempt_identity_sha256, sha256_json, utc_now


def _rehash(run: dict) -> dict:
    run["configuration_sha256"] = sha256_json(
        {"configuration": run["configuration"], "provenance": run["provenance"]}
    )
    for case in run["cases"]:
        if case["attempt_started_at"] is not None:
            case["attempt_configuration_sha256"] = run["configuration_sha256"]
        for attempt in case["attempts"]:
            attempt["configuration_sha256"] = run["configuration_sha256"]
            attempt["raw_attempt_identity_sha256"] = raw_attempt_identity_sha256(
                run_id=run["run_id"],
                manifest_sha256=run["manifest_sha256"],
                case_id=case["case_id"],
                attempt_index=attempt["attempt_index"],
                started_at=attempt["started_at"],
                configuration_sha256=run["configuration_sha256"],
            )
    return run


def _formal_run(run_id: str = "formal") -> dict:
    return make_run(
        run_id,
        {
            "case-a": ("family-a", 100.0),
            "case-b": ("family-b", 200.0),
        },
    )


def _cancelled_attempt(
    run: dict,
    case: dict,
    *,
    reason: str,
    typed_compile_cancellation: bool = False,
) -> dict:
    started_at = utc_now()
    compile_phase = None
    compile_samples = []
    if typed_compile_cancellation:
        compile_phase = deepcopy(case["compile"])
        compile_phase["status"] = "cancelled"
        compile_samples = [deepcopy(case["compile_samples"][0])]
        compile_samples[0]["status"] = "cancelled"
    return {
        "attempt_index": 0,
        "started_at": started_at,
        "archived_at": utc_now(),
        "configuration_sha256": run["configuration_sha256"],
        "raw_attempt_identity_sha256": raw_attempt_identity_sha256(
            run_id=run["run_id"],
            manifest_sha256=run["manifest_sha256"],
            case_id=case["case_id"],
            attempt_index=0,
            started_at=started_at,
            configuration_sha256=run["configuration_sha256"],
        ),
        "status": "cancelled",
        "failure_summary": "scheduler_cancelled",
        "cancellation_reason": reason,
        "cache_hit": False,
        "artifact_sha256": None,
        "binary_sha256": None,
        "remarks_sha256": None,
        "remarks_event_count": None,
        "analysis_sha256": None,
        "attempt_journal_sha256": "a" * 64,
        "attempt_journal_event_count": 1,
        "compile": compile_phase,
        "compile_samples": compile_samples,
        "compile_statistics": None,
        "link": None,
        "analyze": None,
        "measurements": [],
        "samples": [],
        "consistency_passed": False if case["consistency_selected"] else None,
        "consistency_mismatched_metrics": [],
        "diagnostic": "typed cancellation fixture",
    }


def _scheduler_attempt_for_terminal(run: dict, case: dict, terminal: str) -> dict:
    if terminal == "compile":
        return _cancelled_attempt(
            run,
            case,
            reason="scheduler_cancelled",
            typed_compile_cancellation=True,
        )

    attempt = _cancelled_attempt(
        run,
        case,
        reason="scheduler_cancelled",
    )
    attempt.update(
        artifact_sha256=case["artifact_sha256"],
        remarks_sha256=case["remarks_sha256"],
        remarks_event_count=case["remarks_event_count"],
        compile=deepcopy(case["compile"]),
        compile_samples=deepcopy(case["compile_samples"]),
        compile_statistics=deepcopy(case["compile_statistics"]),
    )
    if terminal == "link":
        attempt["link"] = {**deepcopy(case["link"]), "status": "cancelled"}
        return attempt

    attempt.update(
        binary_sha256=case["binary_sha256"],
        link=deepcopy(case["link"]),
    )
    if terminal == "analyze":
        attempt["analyze"] = {**deepcopy(case["analyze"]), "status": "cancelled"}
        return attempt

    if terminal != "run":
        raise AssertionError(f"unknown scheduler terminal fixture: {terminal}")
    cancelled_sample = deepcopy(case["samples"][1])
    cancelled_sample["status"] = "cancelled"
    attempt.update(
        analysis_sha256=case["analysis_sha256"],
        analyze=deepcopy(case["analyze"]),
        measurements=deepcopy(case["measurements"]),
        samples=[deepcopy(case["samples"][0]), cancelled_sample],
    )
    return attempt


def test_formal_gate_requires_every_file_metric_in_each_passed_sample() -> None:
    run = _formal_run()
    non_selected = next(case for case in run["cases"] if not case["consistency_selected"])
    non_selected["samples"][0]["measurements"] = [
        item
        for item in non_selected["samples"][0]["measurements"]
        if item["metric_id"] != "dynamic_load_count"
    ]

    with pytest.raises(ValidationError, match="complete measured file metrics"):
        _require_formal_measurement(run, require_accela_pipeline=True)


def test_formal_gate_metric_superset_is_explicit_and_preserves_base_catalog() -> None:
    run = _formal_run()
    extra_spec = {
        "metric_id": "hotblock_dynamic_count",
        "source": "file",
        "pattern_sha256": sha256_json(r"hotblock=(?P<value>\d+)"),
        "unit": "instructions",
    }
    run["configuration"]["metrics"].append(extra_spec)
    for case in run["cases"]:
        for sample in case["samples"]:
            sample["measurements"].append(
                {
                    "metric_id": "hotblock_dynamic_count",
                    "value": 10.0,
                    "unit": "instructions",
                    "origin": "observed",
                    "availability": "measured",
                    "reason": None,
                }
            )
    _rehash(run)
    validate_document(run)

    with pytest.raises(ValidationError, match="complete rv64gc-qemu-v1 metric catalog"):
        _require_formal_measurement(run)
    _require_formal_measurement(run, allow_metric_superset=True)


def test_formal_gate_allows_external_toolchain_but_requires_accela_bindings() -> None:
    accela = _formal_run()
    _require_formal_measurement(accela, require_accela_pipeline=True)

    external = deepcopy(accela)
    external["run_id"] = "external"
    external["configuration"]["compiler"]["kind"] = "external"
    external["configuration"]["pipeline_profile_file_sha256"] = None
    external["configuration"]["remarks_file_sha256"] = None
    for case in external["cases"]:
        case["remarks_sha256"] = None
        case["remarks_event_count"] = None
        for sample in case["compile_samples"]:
            sample["remarks_sha256"] = None
            sample["remarks_event_count"] = None
    _rehash(external)
    validate_document(external)

    _require_formal_measurement(external)
    with pytest.raises(ValidationError, match="requires BenchmarkCompiler"):
        _require_formal_measurement(external, require_accela_pipeline=True)

    empty_remarks = _formal_run("empty-remarks")
    empty_remarks["cases"][0]["remarks_event_count"] = 0
    with pytest.raises(ValidationError, match="non-empty optimization remarks"):
        _require_formal_measurement(empty_remarks, require_accela_pipeline=True)


def test_formal_gate_rejects_fixed_timeout_even_when_record_is_valid() -> None:
    run = _formal_run()
    run["configuration"]["timeout_policy"] = "fixed"
    run["configuration"]["run_timeout_seconds"] = 1.0
    for case in run["cases"]:
        case["effective_timeout_seconds"] = 1.0
    _rehash(run)
    validate_document(run)

    with pytest.raises(ValidationError, match="initial or baseline-derived"):
        _require_formal_measurement(run)


def test_initial_timeout_record_is_strictly_1800_seconds() -> None:
    run = _formal_run()
    run["configuration"]["run_timeout_seconds"] = 1799.0
    _rehash(run)

    with pytest.raises(ValidationError, match="run=1800"):
        validate_document(run)


def test_baseline_derived_timeout_records_exact_binding_and_formula() -> None:
    run = _formal_run()
    configuration = run["configuration"]
    configuration["timeout_policy"] = "baseline_derived"
    configuration["baseline_timeout_run_id"] = "full-initial"
    configuration["baseline_timeout_run_sha256"] = "a" * 64
    for case in run["cases"]:
        case["effective_timeout_seconds"] = 150.0
        case["timeout_derivation"] = {
            "baseline_run_id": "full-initial",
            "baseline_run_sha256": "a" * 64,
            "baseline_case_status": "passed",
            "baseline_median_duration_ns": 50_000_000_000.0,
        }
    _rehash(run)
    validate_document(run)
    _require_formal_measurement(run, require_accela_pipeline=True)

    tampered = deepcopy(run)
    tampered["cases"][0]["effective_timeout_seconds"] = 149.0
    with pytest.raises(ValidationError, match="120/3x/1800 derivation"):
        validate_document(tampered)


@pytest.mark.parametrize("terminal", ("interrupted", "compile", "link", "analyze", "run"))
def test_formal_gate_allows_only_traceable_safe_resume_history(
    terminal: str,
) -> None:
    run = _formal_run(f"resumed-{terminal}")
    case = run["cases"][0]
    case["attempt_index"] = 1
    if terminal == "interrupted":
        attempt = _cancelled_attempt(
            run,
            case,
            reason="execution_interrupted",
        )
    else:
        attempt = _scheduler_attempt_for_terminal(run, case, terminal)
    case["attempts"] = [attempt]

    validate_document(run)
    _require_formal_measurement(run, require_accela_pipeline=True)


@pytest.mark.parametrize(
    "conflict",
    ("compile_link", "compile_analyze", "link_run", "compile_sample_analyze"),
)
def test_schema_and_formal_gate_reject_conflicting_cancellation_terminals(
    conflict: str,
) -> None:
    run = _formal_run(f"conflicting-cancellation-{conflict}")
    case = run["cases"][0]
    case["attempt_index"] = 1

    if conflict in {"compile_link", "compile_analyze"}:
        attempt = _scheduler_attempt_for_terminal(run, case, "compile")
        if conflict == "compile_link":
            attempt["link"] = {**deepcopy(case["link"]), "status": "cancelled"}
        else:
            attempt["analyze"] = {
                **deepcopy(case["analyze"]),
                "status": "cancelled",
            }
    elif conflict == "link_run":
        attempt = _scheduler_attempt_for_terminal(run, case, "link")
        attempt["samples"] = [
            {**deepcopy(case["samples"][0]), "status": "cancelled"}
        ]
    else:
        attempt = _scheduler_attempt_for_terminal(run, case, "analyze")
        attempt["compile"] = deepcopy(case["compile"])
        attempt["compile_samples"][-1]["status"] = "cancelled"
    case["attempts"] = [attempt]

    with pytest.raises(ValidationError, match="scheduler cancellation"):
        validate_document(run)
    with pytest.raises(ValidationError, match="scheduler cancellation"):
        _require_formal_measurement(run, require_accela_pipeline=True)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda run, attempt: attempt.update(raw_attempt_identity_sha256="f" * 64),
            "untraceable raw identity",
        ),
        (
            lambda run, attempt: attempt.update(configuration_sha256="e" * 64),
            "configuration drift",
        ),
        (
            lambda run, attempt: attempt.update(cancellation_reason="infrastructure_failure"),
            "historical failed attempt",
        ),
        (
            lambda run, attempt: attempt.update(
                status="timeout",
                failure_summary="runtime_timeout",
                cancellation_reason=None,
            ),
            "historical failed attempt",
        ),
    ),
)
def test_formal_gate_rejects_unqualified_resume_history(mutation, message: str) -> None:
    run = _formal_run("resume-tamper")
    case = run["cases"][0]
    case["attempt_index"] = 1
    attempt = _cancelled_attempt(
        run,
        case,
        reason="execution_interrupted",
    )
    case["attempts"] = [attempt]
    mutation(run, attempt)

    with pytest.raises(ValidationError, match=message):
        _require_formal_measurement(run, require_accela_pipeline=True)


def test_formal_gate_rejects_schema_valid_infrastructure_prefix_history() -> None:
    run = _formal_run("infrastructure-prefix-history")
    case = run["cases"][0]
    case["attempt_index"] = 1
    attempt = _cancelled_attempt(
        run,
        case,
        reason="infrastructure_failure",
    )
    attempt.update(
        artifact_sha256=case["artifact_sha256"],
        remarks_sha256=case["remarks_sha256"],
        remarks_event_count=case["remarks_event_count"],
        compile=deepcopy(case["compile"]),
        compile_samples=deepcopy(case["compile_samples"]),
        compile_statistics=deepcopy(case["compile_statistics"]),
        diagnostic="InjectedCommittedPhaseError: after-compile",
    )
    case["attempts"] = [attempt]

    validate_document(run)
    with pytest.raises(ValidationError, match="historical failed attempt"):
        _require_formal_measurement(run, require_accela_pipeline=True)


def test_formal_gate_rejects_cancelled_attempt_masquerading_as_successful_metrics() -> None:
    run = _formal_run("resume-success-masquerade")
    case = run["cases"][0]
    case["attempt_index"] = 1
    attempt = _cancelled_attempt(
        run,
        case,
        reason="scheduler_cancelled",
        typed_compile_cancellation=True,
    )
    attempt.update(
        artifact_sha256=case["artifact_sha256"],
        binary_sha256=case["binary_sha256"],
        remarks_sha256=case["remarks_sha256"],
        remarks_event_count=case["remarks_event_count"],
        analysis_sha256=case["analysis_sha256"],
        compile_statistics=deepcopy(case["compile_statistics"]),
        link=deepcopy(case["link"]),
        analyze=deepcopy(case["analyze"]),
        measurements=deepcopy(case["measurements"]),
        samples=deepcopy(case["samples"]),
    )
    case["attempts"] = [attempt]

    with pytest.raises(ValidationError, match="evidence after its compile terminal"):
        _require_formal_measurement(run, require_accela_pipeline=True)


def test_archived_failure_summary_is_normalized_and_disqualifies_profile() -> None:
    run = _formal_run()
    case = run["cases"][0]
    case["attempt_index"] = 1
    started_at = utc_now()
    case["attempts"].append(
        {
            "attempt_index": 0,
            "started_at": started_at,
            "archived_at": utc_now(),
            "configuration_sha256": run["configuration_sha256"],
            "raw_attempt_identity_sha256": raw_attempt_identity_sha256(
                run_id=run["run_id"],
                manifest_sha256=run["manifest_sha256"],
                case_id=case["case_id"],
                attempt_index=0,
                started_at=started_at,
                configuration_sha256=run["configuration_sha256"],
            ),
            "status": "wrong_output",
            "failure_summary": "correctness_mismatch",
            "cancellation_reason": None,
            "cache_hit": case["cache_hit"],
            "artifact_sha256": case["artifact_sha256"],
            "binary_sha256": case["binary_sha256"],
            "remarks_sha256": case["remarks_sha256"],
            "remarks_event_count": case["remarks_event_count"],
            "analysis_sha256": case["analysis_sha256"],
            "attempt_journal_sha256": "a" * 64,
            "attempt_journal_event_count": 1,
            "compile": deepcopy(case["compile"]),
            "compile_samples": deepcopy(case["compile_samples"]),
            "compile_statistics": deepcopy(case["compile_statistics"]),
            "link": deepcopy(case["link"]),
            "analyze": deepcopy(case["analyze"]),
            "measurements": deepcopy(case["measurements"]),
            "samples": deepcopy(case["samples"]),
            "consistency_passed": case["consistency_passed"],
            "consistency_mismatched_metrics": [],
            "diagnostic": "program output differed",
        }
    )
    validate_document(run)

    with pytest.raises(ValidationError, match="historical failed attempt"):
        _require_formal_measurement(run)

    run["cases"][0]["attempts"][0]["failure_summary"] = "runtime_timeout"
    with pytest.raises(ValidationError, match="summary does not match"):
        validate_document(run)


def test_formal_comparison_requires_identical_retry_configuration() -> None:
    baseline = _formal_run("baseline")
    candidate = _formal_run("candidate")
    candidate["configuration"]["retry_failures"] = True
    _rehash(candidate)
    validate_document(candidate)

    with pytest.raises(ValidationError, match="retry_failures"):
        compare_runs(baseline, candidate)


def test_formal_gate_forbids_retry_failures_even_without_attempt_history() -> None:
    run = _formal_run("formal-retry-forbidden")
    run["configuration"]["retry_failures"] = True
    _rehash(run)
    validate_document(run)

    with pytest.raises(ValidationError, match="forbids retry_failures"):
        _require_formal_measurement(run, require_accela_pipeline=True)


def test_run_options_reject_empty_formal_tool_versions_before_execution(
    benchmark_fixture,
) -> None:
    _, _, _, _, make_options = benchmark_fixture
    options = make_options()

    with pytest.raises(ConfigurationError, match="explicit tool versions"):
        run_benchmark(
            replace(
                options,
                metric_profile_id="rv64gc-qemu-v1",
                tool_versions=(),
            )
        )
