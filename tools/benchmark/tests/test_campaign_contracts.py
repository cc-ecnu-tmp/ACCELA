from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import tools.benchmark.campaign as campaign_module
from tools.benchmark.adapters import StageSpec
from tools.benchmark.campaign import (
    _HOTBLOCK_METRIC_SPECS,
    _failed_run_reason,
    _require_campaign_correctness,
    _study_decisions,
    _validate_campaign_run,
    campaign_task,
    next_campaign_tasks,
    update_campaign_status,
)
from tools.benchmark.errors import ConfigurationError, ValidationError
from tools.benchmark.metrics import rv64gc_qemu_v1
from tools.benchmark.protocol import (
    REQUIRED_ASSETS,
    capture_measurement_protocol,
    verify_measurement_protocol,
)
from tools.benchmark.schema import schema_sha256
from tools.benchmark.util import sha256_json


_RUN_RECORD_SCHEMA_SHA256 = schema_sha256("run-record.v1")


def _protocol(mode: str, digit: str) -> dict:
    return {
        "protocol_id": f"protocol-{mode}",
        "measurement_mode": mode,
        "protocol_sha256": digit * 64,
        "runner_command_sha256": ("a" if mode == "standard_proxy" else "b") * 64,
        "runner_adapter": "wsl",
        "profile_plugin_sha256": "1" * 64,
        "cache_plugin_sha256": "2" * 64,
        "hotblocks_plugin_sha256": "3" * 64,
        "cache_model_sha256": "4" * 64,
    }


def _plan() -> dict:
    return {
        "run_record_schema_sha256": _RUN_RECORD_SCHEMA_SHA256,
        "measurement_protocols": {
            "standard_proxy": _protocol("standard_proxy", "5"),
            "cache_hotblock": _protocol("cache_hotblock", "6"),
        },
        "reference_toolchain": {
            "snapshot_sha256": "7" * 64,
            "source_adapter_sha256": "a" * 64,
            "builtin_header_sha256": "b" * 64,
            "image_id": "sha256:" + "c" * 64,
            "common_tool_versions": {
                "qemu-system-riscv64": "11.0.3",
                "bare-metal-linker": "15.2.0",
                "python": "3.14.6",
                "glib": "2.88.3",
            },
            "accela_jdk_version": "21.0.11",
        },
        "oracle_plan": {"suite_id": "oracle-suite"},
    }


def _task(mode: str = "standard_proxy", compiler: str = "accela_full") -> dict:
    reference = None
    profile_id = "full"
    profile_sha256 = "8" * 64
    if compiler == "gcc_13_3_o2":
        profile_id = "gcc-13.3-o2"
        profile_sha256 = sha256_json(
            {
                "schema": "reference-frontend-profile.v1",
                "compiler_baseline": compiler,
                "compiler_argv_sha256": "d" * 64,
                "source_adapter_sha256": "a" * 64,
                "builtin_header_sha256": "b" * 64,
                "image_id": "sha256:" + "c" * 64,
            }
        )
        reference = {
            "compiler_baseline": compiler,
            "profile_id": profile_id,
            "profile_sha256": profile_sha256,
            "tool": "riscv-gcc",
            "version": "13.3.0",
            "optimization": "-O2",
            "compiler_executable": "sh",
            "compiler_command_sha256": "9" * 64,
            "compiler_argv_sha256": "d" * 64,
        }
    return {
        "task_id": "task-id",
        "run_id": "planned-run",
        "suite_id": "suite",
        "manifest_sha256": "0" * 64,
        "profile_id": profile_id,
        "profile_sha256": profile_sha256,
        "oracle_leg": None,
        "required_evidence_level": "qemu_proxy",
        "measurement_mode": mode,
        "measurement_contract": {
            "metric_profile_id": "rv64gc-qemu-v1",
            "compile_repetitions": 5,
            "reuse_compile_cache": False,
            "additional_metric_specs": (
                deepcopy(_HOTBLOCK_METRIC_SPECS) if mode == "cache_hotblock" else []
            ),
        },
        "compiler_baseline": compiler,
        "reference_compiler_contract": reference,
    }


def _tool_versions(*, external: bool) -> list[dict]:
    versions = [
        {"tool": "qemu-system-riscv64", "actual": "11.0.3", "official_expected": None, "comparison": "unknown"},
        {"tool": "bare-metal-linker", "actual": "15.2.0", "official_expected": None, "comparison": "unknown"},
        {"tool": "python", "actual": "3.14.6", "official_expected": None, "comparison": "unknown"},
        {"tool": "glib", "actual": "2.88.3", "official_expected": None, "comparison": "unknown"},
    ]
    versions.append(
        {"tool": "riscv-gcc", "actual": "13.3.0", "official_expected": "13.3.0", "comparison": "exact"}
        if external
        else {"tool": "accela-jdk", "actual": "21.0.11", "official_expected": None, "comparison": "unknown"}
    )
    return versions


def _run(plan: dict, task: dict) -> dict:
    mode = task["measurement_mode"]
    protocol = plan["measurement_protocols"][mode]
    preset = rv64gc_qemu_v1()
    specs = [
        {
            "metric_id": preset["primary_metric_id"],
            "source": preset["metric_source"],
            "pattern_sha256": sha256_json(preset["metric_pattern"]),
            "unit": preset["metric_unit"],
        },
        *[
            {
                "metric_id": item["metric_id"],
                "source": item["source"],
                "pattern_sha256": None if item["pattern"] is None else sha256_json(item["pattern"]),
                "unit": item["unit"],
            }
            for item in preset["additional"]
        ],
        *deepcopy(task["measurement_contract"]["additional_metric_specs"]),
    ]
    external = task["compiler_baseline"] == "gcc_13_3_o2"
    measurements = [
        {
            "metric_id": item["metric_id"],
            "value": 1,
            "unit": item["unit"],
            "origin": "observed",
            "availability": "measured",
            "reason": None,
        }
        for item in task["measurement_contract"]["additional_metric_specs"]
    ]
    return {
        "run_id": task["run_id"],
        "suite_id": task["suite_id"],
        "manifest_sha256": task["manifest_sha256"],
        "provenance": {
            "pipeline_profile_id": task["profile_id"],
            "pipeline_profile_sha256": task["profile_sha256"],
            "measurement_protocol_id": protocol["protocol_id"],
            "measurement_protocol_sha256": protocol["protocol_sha256"],
            "compiler_artifact_sha256": (
                plan["reference_toolchain"]["snapshot_sha256"] if external else "c" * 64
            ),
        },
        "configuration": {
            "evidence_level": "qemu_proxy",
            "metric_profile_id": "rv64gc-qemu-v1",
            "compile_repetitions": 5,
            "reuse_compile_cache": False,
            "compile_storage_contract": "attempt_local_v1",
            "runner": {
                "kind": "qemu",
                "adapter": protocol["runner_adapter"],
                "command_sha256": protocol["runner_command_sha256"],
                "environment_keys": (
                    ["QEMU_HOTBLOCK_PLUGIN"] if mode == "cache_hotblock" else []
                ),
            },
            "compiler": {
                "kind": "external" if external else "benchmark-compiler",
                "executable": "sh" if external else "java",
                "command_sha256": "9" * 64 if external else "d" * 64,
            },
            "tool_versions": _tool_versions(external=external),
            "metrics": specs,
        },
        "cases": [
            {
                "status": "passed",
                "data_role": "B3",
                "oracle_pair": None,
                "measurements": measurements,
                "samples": [
                    {"status": "passed", "measurements": deepcopy(measurements)}
                ],
            }
        ],
    }


def test_campaign_run_gate_binds_standard_and_cache_protocols(monkeypatch) -> None:
    calls: list[tuple[bool, bool]] = []
    monkeypatch.setattr(
        campaign_module,
        "_require_formal_measurement",
        lambda run, *, require_accela_pipeline=False, allow_metric_superset=False: calls.append(
            (require_accela_pipeline, allow_metric_superset)
        ),
    )
    plan = _plan()
    standard_task = _task()
    _validate_campaign_run(plan, standard_task, _run(plan, standard_task))
    cache_task = _task("cache_hotblock")
    cache_run = _run(plan, cache_task)
    _validate_campaign_run(plan, cache_task, cache_run)
    assert calls == [(True, False), (True, True)]

    missing_metric = deepcopy(cache_run)
    missing_metric["configuration"]["metrics"].pop()
    with pytest.raises(ValidationError, match="normalized hotblock metrics"):
        _validate_campaign_run(plan, cache_task, missing_metric)
    missing_sample_metric = deepcopy(cache_run)
    missing_sample_metric["cases"][0]["samples"][0]["measurements"].pop()
    with pytest.raises(ValidationError, match="passed sample"):
        _validate_campaign_run(plan, cache_task, missing_sample_metric)
    wrong_protocol = deepcopy(cache_run)
    wrong_protocol["provenance"]["measurement_protocol_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="provenance differs"):
        _validate_campaign_run(plan, cache_task, wrong_protocol)


def test_campaign_b1_rejects_retry_history_even_when_final_attempt_passes() -> None:
    run = {
        "configuration": {
            "evidence_level": "qemu_correctness",
            "runner": {"kind": "qemu"},
            "metric_profile_id": None,
            "primary_metric_id": "wall_time_ns",
            "output_contract": "lf_return_trailer",
            "compile_repetitions": 5,
            "reuse_compile_cache": False,
            "compile_storage_contract": "attempt_local_v1",
            "retry_failures": False,
            "compiler": {"kind": "benchmark-compiler"},
            "pipeline_profile_file_sha256": "1" * 64,
            "remarks_file_sha256": "2" * 64,
            "tool_versions": [{"tool": "accela-jdk", "actual": "21.0.11"}],
        },
        "provenance": {
            "pipeline_profile_sha256": "1" * 64,
            "measurement_protocol_id": None,
            "measurement_protocol_sha256": None,
        },
        "cases": [
            {
                "case_id": "b1-retried-case",
                "status": "passed",
                "attempts": [],
                "cache_hit": False,
                "compile": {"status": "ok"},
                "compile_samples": [{"status": "ok"} for _ in range(5)],
                "compile_statistics": {"sample_count": 5},
                "link": {"status": "ok"},
            }
        ],
    }
    _require_campaign_correctness(run)

    run["configuration"]["retry_failures"] = True
    with pytest.raises(ValidationError, match="forbids retry_failures"):
        _require_campaign_correctness(run)
    run["configuration"]["retry_failures"] = False

    run["cases"][0]["attempts"] = [
        {"attempt_index": 0, "status": "wrong_output"}
    ]
    with pytest.raises(ValidationError, match="historical failed attempt"):
        _require_campaign_correctness(run)

    run["cases"][0]["attempts"] = [
        {
            "attempt_index": 0,
            "status": "cancelled",
            "failure_summary": "scheduler_cancelled",
            "cancellation_reason": "infrastructure_failure",
        }
    ]
    with pytest.raises(ValidationError, match="historical failed attempt"):
        _require_campaign_correctness(run)


def test_reference_baseline_requires_exact_command_artifact_version_and_profile(monkeypatch) -> None:
    monkeypatch.setattr(campaign_module, "_require_formal_measurement", lambda *args, **kwargs: None)
    plan = _plan()
    task = _task(compiler="gcc_13_3_o2")
    run = _run(plan, task)
    _validate_campaign_run(plan, task, run)
    for mutate in (
        lambda value: value["configuration"]["compiler"].update(command_sha256="e" * 64),
        lambda value: value["provenance"].update(compiler_artifact_sha256="e" * 64),
        lambda value: value["configuration"]["tool_versions"][-1].update(actual="13.3.1"),
        lambda value: value["provenance"].update(pipeline_profile_sha256="e" * 64),
    ):
        changed = deepcopy(run)
        mutate(changed)
        with pytest.raises(ValidationError, match="provenance differs"):
            _validate_campaign_run(plan, task, changed)


def test_campaign_next_uses_only_running_phase_and_task_query() -> None:
    task_a = {
        "task_id": "a", "suite_role": "B3", "suite_id": "suite",
        "manifest_sha256": "0" * 64, "profile_id": "full", "profile_sha256": "1" * 64,
        "profile_path": "profiles/full.json", "measurement_mode": "standard_proxy",
        "required_evidence_level": "qemu_proxy", "run_id": "run-a",
        "compiler_baseline": "accela_full", "oracle_leg": None,
        "phase_id": "baseline_validation", "dependencies": [],
    }
    task_b = deepcopy(task_a) | {
        "task_id": "b", "run_id": "run-b", "phase_id": "singleton_b2"
    }
    plan = {
        "schema_version": "campaign-plan.v1", "campaign_id": "campaign", "max_workers": 4,
        "run_record_schema_sha256": _RUN_RECORD_SCHEMA_SHA256,
        "tasks": [task_a, task_b],
    }
    status = {
        "campaign_id": "campaign", "plan_sha256": sha256_json(plan),
        "remaining_wall_clock_seconds": 10,
        "tasks": [
            {"task_id": "a", "status": "pending", "selection_state": "selected"},
            {"task_id": "b", "status": "pending", "selection_state": "selected"},
        ],
        "phases": [
            {"phase_id": "baseline_validation", "state": "running", "deadline": "2026-08-10T01:00:00Z"},
            {"phase_id": "singleton_b2", "state": "pending", "deadline": "2026-08-11T01:00:00Z"},
        ],
    }
    assert [item["task_id"] for item in next_campaign_tasks(plan, status)] == ["a"]
    assert campaign_task(plan, task_id="a", field="run_id") == "run-a"
    with pytest.raises(ConfigurationError, match="unknown campaign task field"):
        campaign_task(plan, task_id="a", field="missing")
    stale = {**plan, "run_record_schema_sha256": "f" * 64}
    with pytest.raises(ValidationError, match="run-record schema binding differs"):
        campaign_task(stale, task_id="a")


def test_deadline_closes_awaiting_tasks_and_preserves_intended_run_ids(monkeypatch) -> None:
    phases = [
        {"phase_id": phase_id, "budget_seconds": budget, "unused_budget_destination": None}
        for phase_id, budget in (
            ("baseline_validation", 43_200),
            ("singleton_b2", 86_400),
            ("promotion_b3", 86_400),
            ("final_validation", 43_200),
        )
    ]
    tasks = [
        {
            "task_id": "always", "run_id": "intended-always", "phase_id": "baseline_validation",
            "selection_rule": "always", "profile_id": "full", "dependencies": [],
        },
        {
            "task_id": "awaiting", "run_id": "intended-awaiting", "phase_id": "promotion_b3",
            "selection_rule": "smoke_promoted", "profile_id": "without.x", "dependencies": [],
        },
    ]
    plan = {
        "schema_version": "campaign-plan.v1", "campaign_id": "deadline", "total_budget_seconds": 259_200,
        "run_record_schema_sha256": _RUN_RECORD_SCHEMA_SHA256,
        "parent_plan_sha256": None, "phases": phases, "tasks": tasks,
    }
    decisions = {
        "study_refs": [], "smoke": [], "promoted_profile_ids": [],
        "minimum_top8_satisfied": False, "confirmation": [], "final_profile_ids": [],
        "final_pair_coverage_complete": False,
    }
    monkeypatch.setattr(campaign_module, "load_and_validate", lambda path: plan)
    monkeypatch.setattr(campaign_module, "_load_campaign_runs", lambda plan, paths: {})
    monkeypatch.setattr(campaign_module, "_study_decisions", lambda **kwargs: decisions)
    status = update_campaign_status(
        plan_path=Path("plan.json"), run_paths={}, started_at="2026-08-01T00:00:00Z",
        as_of="2026-08-04T00:00:00Z",
    )
    by_id = {item["task_id"]: item for item in status["tasks"]}
    assert by_id["always"]["run_id"] == "intended-always"
    assert by_id["awaiting"]["run_id"] == "intended-awaiting"
    assert by_id["always"]["missing_reason"] == "not_scheduled"
    assert by_id["awaiting"]["selection_state"] == "awaiting_evidence"
    assert by_id["awaiting"]["status"] == "budget_exhausted"
    assert by_id["awaiting"]["missing_reason"] == "not_scheduled"


def test_failure_categories_are_closed() -> None:
    assert _failed_run_reason({"cases": [{"status": "wrong_output"}]}) == "correctness_failure"
    assert _failed_run_reason({"cases": [{"status": "runtime_error"}]}) == "correctness_failure"
    assert _failed_run_reason({"cases": [{"status": "timeout"}]}) == "timeout"
    assert _failed_run_reason({"cases": [{"status": "compile_error"}]}) == "tool_failure"
    assert _failed_run_reason(
        {"cases": [{"status": "compile_error"}, {"status": "cancelled"}]}
    ) == "tool_failure"
    assert _failed_run_reason(
        {"cases": [{"status": "wrong_output"}, {"status": "cancelled"}]}
    ) == "correctness_failure"
    assert _failed_run_reason({"cases": [{"status": "cancelled"}]}) == "tool_failure"


def _scheduled_task(
    task_id: str,
    *,
    phase_id: str,
    profile_id: str,
    selection_rule: str = "always",
    dependencies: tuple[str, ...] = (),
    suite_role: str = "B2",
) -> dict:
    return {
        "task_id": task_id,
        "suite_role": suite_role,
        "suite_id": f"suite-{suite_role.lower()}",
        "manifest_sha256": "a" * 64,
        "profile_id": profile_id,
        "profile_sha256": "b" * 64,
        "profile_path": f"profiles/{profile_id}.json",
        "phase_id": phase_id,
        "selection_rule": selection_rule,
        "measurement_mode": "standard_proxy",
        "required_evidence_level": "qemu_proxy",
        "run_id": f"run-{task_id}",
        "dependencies": list(dependencies),
        "compiler_baseline": "accela_full",
        "oracle_leg": None,
        "logical_families": [],
    }


def _terminal_run(task: dict, state: str, case_status: str, completed_at: str) -> dict:
    return {
        "run_id": task["run_id"],
        "state": state,
        "started_at": "2026-08-01T00:01:00Z",
        "updated_at": completed_at,
        "completed_at": completed_at,
        "cases": [{"status": case_status}],
    }


def _promotion_decisions() -> dict:
    return {
        "study_refs": [],
        "smoke": [
            {
                "profile_id": "without.bad",
                "geometric_mean_contribution": None,
                "maximum_case_contribution": None,
                "minimum_case_contribution": None,
                "correctness_failures": 1,
                "selected": True,
                "reasons": ["correctness_investigation"],
            }
        ],
        "promoted_profile_ids": ["without.bad"],
        "minimum_top8_satisfied": False,
        "confirmation": [],
        "final_profile_ids": [],
        "final_pair_coverage_complete": False,
    }


def test_failed_singletons_do_not_stop_phase_and_only_correctness_evidence_unlocks_b3(
    monkeypatch,
) -> None:
    baseline = _scheduled_task(
        "baseline", phase_id="baseline_validation", profile_id="full", suite_role="B3"
    )
    full = _scheduled_task("b2-full", phase_id="singleton_b2", profile_id="full")
    bad = _scheduled_task(
        "b2-bad", phase_id="singleton_b2", profile_id="without.bad",
        dependencies=(full["task_id"],),
    )
    timeout = _scheduled_task(
        "b2-timeout", phase_id="singleton_b2", profile_id="without.timeout",
        dependencies=(full["task_id"],),
    )
    tool = _scheduled_task(
        "b2-tool", phase_id="singleton_b2", profile_id="without.tool",
        dependencies=(full["task_id"],),
    )
    other = _scheduled_task(
        "b2-other", phase_id="singleton_b2", profile_id="without.other",
        dependencies=(full["task_id"],),
    )
    diagnostic = _scheduled_task(
        "b3-bad", phase_id="promotion_b3", profile_id="without.bad",
        selection_rule="smoke_promoted", dependencies=(bad["task_id"], baseline["task_id"]),
        suite_role="B3",
    )
    ordinary = _scheduled_task(
        "b3-tool", phase_id="promotion_b3", profile_id="without.tool",
        dependencies=(tool["task_id"], baseline["task_id"]), suite_role="B3",
    )
    plan = {
        "schema_version": "campaign-plan.v1",
        "campaign_id": "continue-after-failure",
        "run_record_schema_sha256": _RUN_RECORD_SCHEMA_SHA256,
        "total_budget_seconds": 259_200,
        "max_workers": 4,
        "parent_plan_sha256": None,
        "phases": [
            {"phase_id": phase_id, "budget_seconds": budget, "unused_budget_destination": None}
            for phase_id, budget in (
                ("baseline_validation", 43_200),
                ("singleton_b2", 86_400),
                ("promotion_b3", 86_400),
                ("final_validation", 43_200),
            )
        ],
        "tasks": [baseline, full, bad, timeout, tool, other, diagnostic, ordinary],
    }
    active_runs = {
        baseline["task_id"]: _terminal_run(
            baseline, "completed", "passed", "2026-08-01T00:05:00Z"
        ),
        full["task_id"]: _terminal_run(
            full, "completed", "passed", "2026-08-01T00:20:00Z"
        ),
        bad["task_id"]: _terminal_run(
            bad, "failed", "wrong_output", "2026-08-01T00:30:00Z"
        ),
        timeout["task_id"]: _terminal_run(
            timeout, "failed", "timeout", "2026-08-01T00:31:00Z"
        ),
        tool["task_id"]: _terminal_run(
            tool, "failed", "compile_error", "2026-08-01T00:32:00Z"
        ),
    }
    monkeypatch.setattr(campaign_module, "load_and_validate", lambda path: plan)
    monkeypatch.setattr(
        campaign_module, "_load_campaign_runs", lambda loaded_plan, paths: active_runs
    )
    monkeypatch.setattr(
        campaign_module, "_study_decisions", lambda **kwargs: _promotion_decisions()
    )

    status = update_campaign_status(
        plan_path=Path("plan.json"), run_paths={}, started_at="2026-08-01T00:00:00Z",
        as_of="2026-08-01T00:40:00Z",
    )
    phase = next(item for item in status["phases"] if item["phase_id"] == "singleton_b2")
    assert phase["state"] == "running"
    assert status["state"] == "running"
    task_status = {item["task_id"]: item for item in status["tasks"]}
    assert task_status[bad["task_id"]]["missing_reason"] == "correctness_failure"
    assert task_status[timeout["task_id"]]["missing_reason"] == "timeout"
    assert task_status[tool["task_id"]]["missing_reason"] == "tool_failure"
    assert [item["task_id"] for item in next_campaign_tasks(plan, status)] == [other["task_id"]]

    active_runs[other["task_id"]] = _terminal_run(
        other, "completed", "passed", "2026-08-01T00:45:00Z"
    )
    status = update_campaign_status(
        plan_path=Path("plan.json"), run_paths={}, started_at="2026-08-01T00:00:00Z",
        as_of="2026-08-01T00:50:00Z",
    )
    phases = {item["phase_id"]: item for item in status["phases"]}
    assert phases["singleton_b2"]["state"] == "failed"
    assert phases["promotion_b3"]["state"] == "running"
    assert status["state"] == "running"
    task_status = {item["task_id"]: item for item in status["tasks"]}
    assert task_status[ordinary["task_id"]]["missing_reason"] == "dependency_failed"
    assert [item["task_id"] for item in next_campaign_tasks(plan, status)] == [
        diagnostic["task_id"]
    ]


def test_smoke_top8_fill_requires_measurement_but_correctness_anomaly_is_promoted(
    monkeypatch,
) -> None:
    profiles = ("without.correctness", "without.measured", "without.no-evidence")
    tasks = [
        {
            "task_id": "baseline", "phase_id": "singleton_b2", "profile_id": "full",
            "suite_role": "B2", "oracle_leg": None,
        },
        *[
            {
                "task_id": profile, "phase_id": "singleton_b2", "profile_id": profile,
                "suite_role": "B2", "oracle_leg": None,
            }
            for profile in profiles
        ],
    ]
    plan = {
        "initial_matrix_sha256": "c" * 64,
        "tasks": tasks,
        "promotion": {
            "smoke_geometric_mean_minimum": 1.005,
            "smoke_any_case_minimum": 1.10,
            "smoke_regression_ratio_below": 0.97,
            "minimum_promoted_profiles": 8,
            "final_profile_count": 5,
        },
        "final_pair_families": [],
    }
    study = {
        "schema_version": "ablation-study.v1",
        "matrix_sha256": "c" * 64,
        "study_id": "smoke",
        "baseline_run_id": "run-baseline",
        "variants": [
            {
                "profile_id": "without.correctness", "run_id": "run-correctness",
                "correctness_failures": 1, "case_geometric_mean_contribution": None,
                "per_cases": [],
            },
            {
                "profile_id": "without.measured", "run_id": "run-measured",
                "correctness_failures": 0, "case_geometric_mean_contribution": 1.0,
                "per_cases": [{"contribution_ratio": 1.0}],
            },
            {
                "profile_id": "without.no-evidence", "run_id": "run-no-evidence",
                "correctness_failures": 0, "case_geometric_mean_contribution": None,
                "per_cases": [],
            },
        ],
    }
    monkeypatch.setattr(campaign_module, "load_and_validate", lambda path: study)
    runs = {
        "baseline": {"run_id": "run-baseline"},
        "without.correctness": {"run_id": "run-correctness"},
        "without.measured": {"run_id": "run-measured"},
        "without.no-evidence": {"run_id": "run-no-evidence"},
    }
    decisions = _study_decisions(
        plan=plan, runs=runs, study_paths={"singleton_b2": Path("study.json")}
    )
    rows = {item["profile_id"]: item for item in decisions["smoke"]}
    assert rows["without.correctness"]["reasons"] == ["correctness_investigation"]
    assert rows["without.measured"]["reasons"] == ["minimum_top8_fill"]
    assert rows["without.no-evidence"]["selected"] is False
    assert decisions["promoted_profile_ids"] == [
        "without.correctness", "without.measured"
    ]


def test_cache_hotblock_protocol_requires_physical_hotblock_placeholder(tmp_path: Path) -> None:
    tool = tmp_path / "tool"
    tool.write_bytes(b"tool")
    assets = {key: tool for key in REQUIRED_ASSETS}
    runner = StageSpec(
        "qemu", "host",
        (
            "{qemu_binary}", "{runner_executable}", "{profile_plugin_binary}",
            "{cache_plugin_binary}", "{hotblocks_plugin_binary}", "{binary}",
            "{input}", "{metric_file}",
        ),
        {},
    )
    monkeypatch_runner = runner
    # Use a real executable only for QEMU --version; all other assets remain
    # ordinary content-addressed files.
    import sys

    assets["qemu_binary"] = Path(sys.executable)
    protocol = capture_measurement_protocol(
        protocol_id="cache-hotblock-test", assets=assets, runner=monkeypatch_runner,
        machine="virt", cpu_model="rv64", memory="128M", measurement_mode="cache_hotblock",
    )
    assert protocol["measurement_mode"] == "cache_hotblock"
    assert protocol["input_transport"] == {
        "kind": "fw_cfg_dma",
        "item_name": "opt/accela/sysy-input",
        "exact_bytes": True,
        "eof": "size_delimited",
        "max_input_size_bytes": 4_294_967_295,
        "guest_buffer_size_bytes": 4_096,
        "guest_buffer_section": ".sysy_input_transport",
        "transport_section_size_bytes": 4_112,
    }
    missing = StageSpec(
        "qemu", "host",
        (
            "{qemu_binary}", "{runner_executable}", "{profile_plugin_binary}",
            "{cache_plugin_binary}", "{binary}", "{input}", "{metric_file}",
        ),
        {},
    )
    with pytest.raises(ConfigurationError, match="hotblocks_plugin_binary"):
        capture_measurement_protocol(
            protocol_id="invalid-cache-hotblock", assets=assets, runner=missing,
            machine="virt", cpu_model="rv64", memory="128M", measurement_mode="cache_hotblock",
        )

    no_input = StageSpec(
        "qemu", "host",
        (
            "{qemu_binary}", "{runner_executable}", "{profile_plugin_binary}",
            "{cache_plugin_binary}", "{hotblocks_plugin_binary}", "{binary}",
            "{metric_file}",
        ),
        {"ACCELA_INPUT": "{input}"},
    )
    with pytest.raises(ConfigurationError, match="physical.*input"):
        capture_measurement_protocol(
            protocol_id="invalid-input-transport", assets=assets, runner=no_input,
            machine="virt", cpu_model="rv64", memory="128M", measurement_mode="cache_hotblock",
        )

    complete_command = (
        "{qemu_binary}", "{runner_executable}", "{profile_plugin_binary}",
        "{cache_plugin_binary}", "{hotblocks_plugin_binary}", "{binary_host}",
        "{input_wsl}", "{metric_file_host}",
    )
    for missing in ("binary", "metric_file"):
        without_physical = StageSpec(
            "qemu", "host",
            tuple(value for value in complete_command if missing not in value),
            {f"ACCELA_{missing.upper()}": "{" + missing + "}"},
        )
        with pytest.raises(ConfigurationError, match=rf"physical.*{missing}"):
            capture_measurement_protocol(
                protocol_id=f"invalid-{missing}-transport", assets=assets,
                runner=without_physical, machine="virt", cpu_model="rv64",
                memory="128M", measurement_mode="cache_hotblock",
            )

    variants = StageSpec("qemu", "host", complete_command, {})
    capture_measurement_protocol(
        protocol_id="physical-path-variants", assets=assets, runner=variants,
        machine="virt", cpu_model="rv64", memory="128M", measurement_mode="cache_hotblock",
    )

    legacy = deepcopy(protocol)
    legacy.pop("input_transport")
    with pytest.raises(ValidationError, match="input_transport.*required property"):
        verify_measurement_protocol(legacy, assets=assets, runner=runner)

    drifted = deepcopy(protocol)
    drifted["input_transport"]["item_name"] = "opt/accela/other"
    with pytest.raises(ValidationError, match="opt/accela/sysy-input"):
        verify_measurement_protocol(drifted, assets=assets, runner=runner)
