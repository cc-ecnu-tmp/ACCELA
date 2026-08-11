from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tools.benchmark import cli
from tools.benchmark import candidates as candidate_module
from tools.benchmark.campaign import (
    campaign_status_chain,
    enforce_terminal_task_immutability,
    ready_campaign_task_ids,
    require_formal_suite_contract,
)
from tools.benchmark.errors import ConfigurationError, ValidationError
from tools.benchmark.report import build_candidate_screening_report
from tools.benchmark.schema import (
    schema_sha256,
    load_and_validate,
    validate_candidate_remark_jsonl,
    validate_document,
)
from tools.benchmark.util import raw_attempt_identity_sha256, sha256_file, sha256_json
from tools.benchmark.tests.test_stats_ablation_report import make_run
from tools.benchmark.execution import VerifiedRunRawEvidence


def _input_files(root: Path, *names: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        result[name] = path
    return result


def test_candidate_subcommands_are_registered() -> None:
    parser = cli.build_parser()
    root = next(
        action
        for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    )
    candidates_parser = root.choices["candidates"]
    commands = next(
        action
        for action in candidates_parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    )
    assert {
        "profiles",
        "screen",
        "analyze",
        "study",
        "oracle-capture",
        "campaign-plan",
        "campaign-status",
        "campaign-finalize",
        "final",
    }.issubset(commands.choices)
    assert "pre-implementation PassRegistry export" in commands.choices[
        "screen"
    ].format_help()
    for command in ("profiles", "analyze", "campaign-plan", "campaign-finalize"):
        assert "post-implementation executable PassRegistry export" in commands.choices[
            command
        ].format_help()


def test_candidate_profiles_dispatch_passes_exact_workspace_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _input_files(tmp_path, "catalog.json", "pass-registry.json")
    received: dict[str, Any] = {}

    def fake_generate_candidate_profile_matrix(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-profile-matrix.v1",
            "profiles": [],
            "schedule": [],
        }

    monkeypatch.setattr(
        cli, "generate_candidate_profile_matrix", fake_generate_candidate_profile_matrix
    )
    args = cli.build_parser().parse_args(
        [
            "candidates",
            "profiles",
            "--registry",
            "catalog.json",
            "--pass-registry",
            "pass-registry.json",
            "--workspace-root",
            str(tmp_path),
            "--matrix-id",
            "matrix-1",
            "--top-candidate",
            "candidate.a",
            "--pair",
            "candidate.a+candidate.b",
            "--output-dir",
            "profiles",
        ]
    )

    assert cli.dispatch(args) == 0
    assert received == {
        "catalog_path": tmp_path / "catalog.json",
        "pass_registry_path": tmp_path / "pass-registry.json",
        "matrix_id": "matrix-1",
        "workspace_root": tmp_path,
        "output_directory": Path("profiles"),
        "pairs": (("candidate.a", "candidate.b"),),
        "top_candidates": ("candidate.a",),
    }


def test_candidate_profile_outputs_are_symlink_safe_and_create_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {
        "schema_version": "candidate-catalog.v1",
        "candidates": [{"candidate_id": "candidate.a"}],
    }
    pass_registry = {"schema_version": "pass-registry.v2", "passes": []}

    def fake_load(path: Path, version: str, *, label: str) -> dict[str, Any]:
        del path, label
        return deepcopy(
            catalog if version == "candidate-catalog.v1" else pass_registry
        )

    monkeypatch.setattr(candidate_module, "_load_version", fake_load)
    monkeypatch.setattr(candidate_module, "_require_catalog_registry", lambda *_: None)
    arguments = {
        "catalog_path": Path("catalog.json"),
        "pass_registry_path": Path("pass-registry.json"),
        "matrix_id": "matrix-1",
        "workspace_root": tmp_path,
        "output_directory": Path("generated"),
    }
    first = candidate_module.generate_candidate_profile_matrix(**arguments)
    first_bytes = (tmp_path / "generated" / "matrix.json").read_bytes()
    second = candidate_module.generate_candidate_profile_matrix(**arguments)
    assert first == second
    assert (tmp_path / "generated" / "matrix.json").read_bytes() == first_bytes

    (tmp_path / "generated" / "matrix.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="different bytes"):
        candidate_module.generate_candidate_profile_matrix(**arguments)

    physical = tmp_path / "physical-output"
    physical.mkdir()
    linked = tmp_path / "linked-output"
    linked.symlink_to(physical, target_is_directory=True)
    with pytest.raises(ValidationError, match="symbolic link"):
        candidate_module.generate_candidate_profile_matrix(
            **{**arguments, "output_directory": Path("linked-output")}
        )

    stale_output = tmp_path / "stale-output"
    stale_output.mkdir()
    (stale_output / "stale-link").symlink_to(physical, target_is_directory=True)
    with pytest.raises(ValidationError, match="contains a symbolic link"):
        candidate_module.generate_candidate_profile_matrix(
            **{**arguments, "output_directory": Path("stale-output")}
        )

    extra_output = tmp_path / "extra-output"
    (extra_output / "profiles").mkdir(parents=True)
    (extra_output / "unexpected").mkdir()
    with pytest.raises(ConfigurationError, match="unmanaged directories"):
        candidate_module.generate_candidate_profile_matrix(
            **{**arguments, "output_directory": Path("extra-output")}
        )


def test_candidate_oracle_capture_dispatch_passes_exact_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _input_files(
        tmp_path,
        "evidence.json",
        "plan.json",
        "baseline.json",
        "optimized.json",
    )
    (tmp_path / "state").mkdir()
    received: dict[str, Any] = {}

    def fake_capture_candidate_oracle(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-oracle-capture.v1",
            "candidates": [],
        }

    monkeypatch.setattr(cli, "capture_candidate_oracle", fake_capture_candidate_oracle)
    args = cli.build_parser().parse_args(
        [
            "candidates",
            "oracle-capture",
            "--workspace-root",
            str(tmp_path),
            "--evidence",
            "evidence.json",
            "--oracle-plan",
            "plan.json",
            "--baseline",
            "baseline.json",
            "--optimized",
            "optimized.json",
            "--state-root",
            "state",
            "--capture-id",
            "capture-1",
            "--output",
            "capture.json",
        ]
    )

    assert cli.dispatch(args) == 0
    assert received == {
        "candidate_evidence_path": tmp_path / "evidence.json",
        "oracle_plan_path": tmp_path / "plan.json",
        "baseline_path": tmp_path / "baseline.json",
        "optimized_path": tmp_path / "optimized.json",
        "state_root": tmp_path / "state",
        "workspace_root": tmp_path,
        "capture_id": "capture-1",
    }
    assert (tmp_path / "capture.json").is_file()


def test_candidate_oracle_capture_replays_both_legs_from_one_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, _, captured = _screening_documents()
    baseline = make_run("oracle-baseline", {"case": ("family", 100.0)})
    optimized = deepcopy(baseline)
    optimized["run_id"] = "oracle-optimized"
    baseline["provenance"]["pipeline_profile_id"] = "full-v2"
    optimized["provenance"]["pipeline_profile_id"] = "full-v2"
    plan = {
        "evidence_class": "cleanroom",
        "pairs": [{} for _ in range(99)],
        "baseline_run_id": baseline["run_id"],
        "optimized_run_id": optimized["run_id"],
        "pipeline_profile": {
            "profile_id": "full-v2",
            "profile_sha256": baseline["provenance"]["pipeline_profile_sha256"],
        },
    }
    rows = [
        structure["sizes"][size]
        for candidate in captured["candidates"]
        for structure in candidate["structures"]
        for size in ("small", "medium", "large")
    ]
    paths = {
        tmp_path / "evidence.json": evidence,
        tmp_path / "plan.json": plan,
        tmp_path / "baseline.json": baseline,
        tmp_path / "optimized.json": optimized,
    }
    for path in paths:
        path.write_text("{}\n", encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()

    monkeypatch.setattr(
        candidate_module,
        "_load_version",
        lambda path, *_args, **_kwargs: deepcopy(paths[Path(path)]),
    )
    monkeypatch.setattr(candidate_module, "_oracle_pair_rows", lambda *_: rows)
    monkeypatch.setattr(candidate_module, "_require_formal_measurement", lambda *_args, **_kwargs: None)
    observed_state_roots: list[Path] = []

    def fake_verify(path: Path, observed_state_root: Path) -> VerifiedRunRawEvidence:
        run = paths[Path(path)]
        observed_state_roots.append(observed_state_root)
        index = 1 if run["run_id"] == baseline["run_id"] else 2
        return VerifiedRunRawEvidence(
            document={
                "schema_version": "benchmark-run-raw-evidence.v1",
                **_raw_run_ref(run["run_id"], sha256_json(run), index),
                "cases": [],
            },
            current_remark_paths={},
        )

    monkeypatch.setattr(candidate_module, "verify_run_raw_evidence", fake_verify)
    first = candidate_module.capture_candidate_oracle(
        candidate_evidence_path=tmp_path / "evidence.json",
        oracle_plan_path=tmp_path / "plan.json",
        baseline_path=tmp_path / "baseline.json",
        optimized_path=tmp_path / "optimized.json",
        state_root=state_root,
        workspace_root=tmp_path,
        capture_id="capture-1",
    )
    second = candidate_module.capture_candidate_oracle(
        candidate_evidence_path=tmp_path / "evidence.json",
        oracle_plan_path=tmp_path / "plan.json",
        baseline_path=tmp_path / "baseline.json",
        optimized_path=tmp_path / "optimized.json",
        state_root=state_root,
        workspace_root=tmp_path,
        capture_id="capture-1",
    )
    assert sha256_json(first) == sha256_json(second)
    assert observed_state_roots == [state_root, state_root, state_root, state_root]
    assert first["raw_evidence"]["baseline"]["run_id"] == baseline["run_id"]
    assert first["raw_evidence"]["optimized"]["run_id"] == optimized["run_id"]

    for run in (baseline, optimized):
        run["configuration"]["timeout_policy"] = "baseline_derived"
        run["configuration"]["baseline_timeout_run_sha256"] = "d" * 64
        run["configuration"]["baseline_timeout_run_id"] = "unrelated-timeout-baseline"
        for case in run["cases"]:
            case["timeout_derivation"] = {
                "baseline_run_id": "unrelated-timeout-baseline",
                "baseline_run_sha256": "d" * 64,
                "baseline_case_status": "passed",
                "baseline_median_duration_ns": 1,
            }
    with pytest.raises(ValidationError, match="exact initial timeout evidence"):
        candidate_module.capture_candidate_oracle(
            candidate_evidence_path=tmp_path / "evidence.json",
            oracle_plan_path=tmp_path / "plan.json",
            baseline_path=tmp_path / "baseline.json",
            optimized_path=tmp_path / "optimized.json",
            state_root=state_root,
            workspace_root=tmp_path,
            capture_id="capture-1",
        )

    for run in (baseline, optimized):
        run["configuration"]["timeout_policy"] = "initial"
        run["configuration"]["baseline_timeout_run_sha256"] = None
        run["configuration"]["baseline_timeout_run_id"] = None
    with pytest.raises(ValidationError, match="without baseline derivations"):
        candidate_module.capture_candidate_oracle(
            candidate_evidence_path=tmp_path / "evidence.json",
            oracle_plan_path=tmp_path / "plan.json",
            baseline_path=tmp_path / "baseline.json",
            optimized_path=tmp_path / "optimized.json",
            state_root=state_root,
            workspace_root=tmp_path,
            capture_id="capture-1",
        )
    for run in (baseline, optimized):
        for case in run["cases"]:
            case["timeout_derivation"] = None

    tampered = deepcopy(first)
    structure = tampered["candidates"][1]["structures"][0]
    for size in ("small", "medium", "large"):
        pair = structure["sizes"][size]
        pair["optimized_metric_value"] = pair["baseline_metric_value"] / 2.0
        pair["speedup"] = 2.0
    structure["geometric_mean_upper_bound"] = 2.0
    capture_path = tmp_path / "capture.json"
    capture_path.write_text("{}\n", encoding="utf-8")
    paths[capture_path] = tampered

    def fake_frozen(
        _root: Path,
        artifact: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return (
            evidence
            if artifact["path"] == first["sources"]["candidate_evidence"]["path"]
            else plan
        )

    monkeypatch.setattr(candidate_module, "_load_frozen_artifact", fake_frozen)
    with pytest.raises(ValidationError, match="differs from replayed"):
        candidate_module._load_and_reverify_candidate_oracle_capture(
            capture_path=capture_path,
            workspace_root=tmp_path,
        )


def test_candidate_campaign_plan_dispatch_passes_all_six_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [
        "catalog.json",
        "pass-registry.json",
        "matrix.json",
        "screening.json",
        "protocol.json",
        "compiler.jar",
        *(f"{role.lower()}.json" for role in ("B1", "B2", "B3", "B4", "B5", "B6")),
    ]
    _input_files(tmp_path, *names)
    (tmp_path / "raw-state").mkdir()
    received: dict[str, Any] = {}

    def fake_build_candidate_campaign_plan(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-campaign-plan.v1",
            "tasks": [],
        }

    monkeypatch.setattr(
        cli, "build_candidate_campaign_plan", fake_build_candidate_campaign_plan
    )
    arguments = [
        "candidates",
        "campaign-plan",
        "--workspace-root",
        str(tmp_path),
        "--registry",
        "catalog.json",
        "--pass-registry",
        "pass-registry.json",
        "--matrix",
        "matrix.json",
        "--screening",
        "screening.json",
    ]
    for role in ("B1", "B2", "B3", "B4", "B5", "B6"):
        arguments.extend(["--manifest", f"{role}={role.lower()}.json"])
    arguments.extend(
        [
            "--measurement-protocol",
            "protocol.json",
            "--compiler-artifact",
            "compiler.jar",
            "--raw-state-root",
            "raw-state",
            "--campaign-id",
            "campaign-1",
            "--output",
            "plan.json",
        ]
    )

    assert cli.dispatch(cli.build_parser().parse_args(arguments)) == 0
    assert received["suite_paths"] == {
        role: tmp_path / f"{role.lower()}.json"
        for role in ("B1", "B2", "B3", "B4", "B5", "B6")
    }
    assert received["compiler_artifact_path"] == tmp_path / "compiler.jar"
    assert received["raw_state_root"] == tmp_path / "raw-state"
    assert received["campaign_id"] == "campaign-1"


def test_candidate_campaign_protocol_binding_is_role_specific() -> None:
    plan = {
        "measurement_protocol": {
            "protocol_id": "standard-proxy",
            "protocol_sha256": "a" * 64,
        }
    }
    task = {"task_id": "run.B1.full", "data_role": "B1"}
    run = {
        "provenance": {
            "measurement_protocol_id": None,
            "measurement_protocol_sha256": None,
        }
    }
    candidate_module._require_candidate_campaign_protocol_binding(
        plan=plan, task=task, run=run
    )

    run["provenance"]["measurement_protocol_id"] = "standard-proxy"
    run["provenance"]["measurement_protocol_sha256"] = "a" * 64
    with pytest.raises(ValidationError, match="B1 correctness protocol binding"):
        candidate_module._require_candidate_campaign_protocol_binding(
            plan=plan, task=task, run=run
        )

    task = {"task_id": "run.B2.full", "data_role": "B2"}
    candidate_module._require_candidate_campaign_protocol_binding(
        plan=plan, task=task, run=run
    )
    run["provenance"]["measurement_protocol_id"] = None
    run["provenance"]["measurement_protocol_sha256"] = None
    with pytest.raises(ValidationError, match="B2 standard-proxy protocol binding"):
        candidate_module._require_candidate_campaign_protocol_binding(
            plan=plan, task=task, run=run
        )


@pytest.mark.parametrize(
    ("compiler_baseline", "profile_id", "tool", "version"),
    [
        ("gcc_13_3_o2", "gcc-13.3-o2", "riscv-gcc", "13.3.0"),
        ("clang_18_o3", "clang-18-o3", "clang", "18.1.3"),
    ],
)
def test_candidate_reference_run_binds_exact_frozen_profile_sha(
    compiler_baseline: str,
    profile_id: str,
    tool: str,
    version: str,
) -> None:
    task = {"reference_profile_id": profile_id}
    freeze = {
        "reference_toolchain": {
            "snapshot": {"physical_sha256": "a" * 64},
            "baselines": [
                {
                    "compiler_baseline": compiler_baseline,
                    "profile_id": profile_id,
                    "profile_sha256": "b" * 64,
                    "compiler_executable": "sh",
                    "compiler_command_sha256": "c" * 64,
                    "tool": tool,
                    "version": version,
                }
            ],
        }
    }
    run = {
        "configuration": {
            "compiler": {
                "kind": "external",
                "executable": "sh",
                "command_sha256": "c" * 64,
            },
            "tool_versions": [
                {
                    "tool": tool,
                    "actual": version,
                    "official_expected": version,
                    "comparison": "exact",
                }
            ],
        },
        "provenance": {
            "compiler_artifact_sha256": "a" * 64,
            "pipeline_profile_sha256": "b" * 64,
        },
    }
    candidate_module._require_frozen_reference_run(run, task, freeze)
    run["provenance"]["pipeline_profile_sha256"] = "d" * 64
    with pytest.raises(
        ValidationError, match=f"frozen {compiler_baseline} contract"
    ):
        candidate_module._require_frozen_reference_run(run, task, freeze)


def test_candidate_diagnostic_sequence_rejects_supplied_suffix_evidence() -> None:
    source = {
        "task_id": "study.B3",
        "status": "completed",
        "completed_at": "2026-08-11T00:00:00Z",
    }
    first = {
        "task_id": "diagnostic.pair.a+b",
        "status": "pending",
        "evidence_sha256": None,
        "started_at": None,
        "completed_at": None,
    }
    second = {
        "task_id": "diagnostic.pair.a+c",
        "status": "completed",
        "evidence_sha256": "a" * 64,
        "started_at": "2026-08-11T00:02:00Z",
        "completed_at": "2026-08-11T00:03:00Z",
    }
    with pytest.raises(ValidationError, match="leapfrogs unfinished predecessor"):
        candidate_module._validate_candidate_diagnostic_sequence(
            source_status=source, tasks=[first, second]
        )

    first.update(
        status="failed",
        evidence_sha256="b" * 64,
        started_at="2026-08-11T00:01:00Z",
        completed_at="2026-08-11T00:01:30Z",
    )
    candidate_module._validate_candidate_diagnostic_sequence(
        source_status=source, tasks=[first, second]
    )


def test_candidate_campaign_plan_builds_valid_serial_b4_b6_dag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = Path(__file__).resolve().parents[3]
    catalog_relative = Path(
        "docs/optimization/data/candidates/candidate-catalog.2026-r1.v1.json"
    )
    pass_registry_relative = Path(
        "docs/optimization/data/candidates/pass-registry.executable-2026-r1.v2.json"
    )
    matrix_relative = Path(
        "docs/optimization/data/candidates/profiles-2026-r1/matrix.json"
    )
    screening_relative = Path(
        "docs/optimization/data/candidates/candidate-screening.v1.json"
    )
    protocol_relative = Path("docs/optimization/data/measurement-protocol.v1.json")
    manifest_relatives = {
        "B1": Path("docs/optimization/data/manifests/b1-official-functional-2026.manifest.json"),
        "B2": Path("docs/optimization/data/manifests/b2-family-smoke.manifest.json"),
        "B3": Path("docs/optimization/data/manifests/b3-official-performance-2026.manifest.json"),
        "B4": Path("docs/optimization/data/manifests/b4-official-performance-2025-preliminary.manifest.json"),
        "B5": Path("docs/optimization/data/manifests/b5-structural-variants.manifest.json"),
        "B6": Path("docs/optimization/data/manifests/b6-mature-benchmarks.manifest.json"),
    }

    matrix = json.loads((source_root / matrix_relative).read_text(encoding="utf-8"))
    screening = load_and_validate(source_root / screening_relative)
    copied_relatives = {
        catalog_relative,
        pass_registry_relative,
        matrix_relative,
        screening_relative,
        protocol_relative,
        Path(screening["base_pass_registry"]["path"]),
        *manifest_relatives.values(),
        *(Path(item["path"]) for item in matrix["profiles"]),
    }
    for relative in copied_relatives:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source_root / relative).read_bytes())

    compiler_artifact = tmp_path / "build/compiler.jar"
    compiler_artifact.parent.mkdir()
    compiler_artifact.write_bytes(b"candidate campaign test artifact\n")
    raw_state_root = tmp_path / "raw-state"
    raw_state_root.mkdir()
    monkeypatch.setattr(
        candidate_module,
        "_load_and_reverify_candidate_screening",
        lambda **kwargs: load_and_validate(kwargs["screening_path"]),
    )
    monkeypatch.setattr(
        candidate_module,
        "_clean_repository_identity",
        lambda *_args, **_kwargs: ("a" * 40, "b" * 40),
    )

    plan = candidate_module.build_candidate_campaign_plan(
        catalog_path=tmp_path / catalog_relative,
        pass_registry_path=tmp_path / pass_registry_relative,
        matrix_path=tmp_path / matrix_relative,
        screening_path=tmp_path / screening_relative,
        suite_paths={
            role: tmp_path / relative
            for role, relative in manifest_relatives.items()
        },
        measurement_protocol_path=tmp_path / protocol_relative,
        compiler_artifact_path=compiler_artifact,
        raw_state_root=raw_state_root,
        workspace_root=tmp_path,
        campaign_id="candidate-campaign-dag-test",
    )
    assert validate_document(deepcopy(plan)) == plan

    by_id = {item["task_id"]: item for item in plan["tasks"]}
    candidate_ids = plan["qualified_candidate_ids"]
    first_candidate_id = candidate_ids[0]
    for task_id, field, replacement, message in (
        (
            "run.B3.gcc",
            "dependencies",
            ["freeze"],
            "B3 reference serialization",
        ),
        (
            f"run.B3.{first_candidate_id}",
            "terminal_dependencies",
            [f"run.B1.{first_candidate_id}", "run.B3.gcc"],
            "B3 selection/serialization",
        ),
        (
            "study.B3",
            "terminal_dependencies",
            [
                "run.B3.clang",
                *[f"run.B3.{candidate_id}" for candidate_id in candidate_ids],
            ],
            "B3 study serialization",
        ),
    ):
        tampered = deepcopy(plan)
        next(item for item in tampered["tasks"] if item["task_id"] == task_id)[
            field
        ] = replacement
        with pytest.raises(ValidationError, match=message):
            validate_document(tampered)

    for task in plan["tasks"]:
        dependencies = [*task["dependencies"], *task["terminal_dependencies"]]
        assert len(dependencies) == len(set(dependencies)), task["task_id"]

    b2_full = by_id["run.B2.full"]
    assert b2_full["dependencies"] == ["run.B1.full"]
    assert b2_full["terminal_dependencies"] == [
        f"run.B1.{candidate_id}" for candidate_id in candidate_ids
    ]

    def ready_with(overrides: dict[str, str]) -> list[str]:
        return ready_campaign_task_ids(
            tasks=plan["tasks"],
            statuses=[
                {
                    "task_id": task["task_id"],
                    "status": overrides.get(task["task_id"], "pending"),
                }
                for task in plan["tasks"]
            ],
        )

    assert ready_with({"run.B1.full": "failed"}) == []
    b1_terminal = {"run.B1.full": "completed"}
    b1_terminal.update(
        {
            f"run.B1.{candidate_id}": (
                "failed" if index == 0 else "completed"
            )
            for index, candidate_id in enumerate(candidate_ids)
        }
    )
    assert ready_with(b1_terminal) == ["run.B2.full"]

    ordered_task_ids = [item["task_id"] for item in plan["tasks"]]
    for role in ("B4", "B5", "B6"):
        full_task_id = f"run.{role}.full"
        states = {
            task_id: "completed"
            for task_id in ordered_task_ids[: ordered_task_ids.index(full_task_id)]
        }
        assert ready_with(states) == [full_task_id]
        states[full_task_id] = "completed"
        previous_profile = None
        for index, candidate_id in enumerate(candidate_ids):
            task_id = f"run.{role}.{candidate_id}"
            task = by_id[task_id]
            assert task["dependencies"] == ["study.B3", full_task_id]
            assert task["terminal_dependencies"] == (
                [] if previous_profile is None else [previous_profile]
            )
            assert ready_with(states) == [task_id]
            states[task_id] = "failed" if index == 0 else "completed"
            previous_profile = task_id
        assert ready_with(states) == [f"study.{role}"]

    def pending_rows() -> dict[str, dict[str, Any]]:
        return {
            task["task_id"]: {
                "task_id": task["task_id"],
                "status": "pending",
                "evidence_sha256": None,
                "completed_at": None,
            }
            for task in plan["tasks"]
        }

    def complete(
        rows: dict[str, dict[str, Any]],
        task_id: str,
        completed_at: str,
        *,
        status: str = "completed",
    ) -> None:
        rows[task_id]["status"] = status
        rows[task_id]["completed_at"] = completed_at

    def ready_rows(rows: dict[str, dict[str, Any]]) -> list[str]:
        ready = ready_campaign_task_ids(
            tasks=plan["tasks"], statuses=list(rows.values())
        )
        assert len(ready) <= 1
        return ready

    b1_gate_rows = pending_rows()
    complete(b1_gate_rows, "run.B1.full", "2026-08-11T00:00:00Z")
    for index, candidate_id in enumerate(candidate_ids):
        complete(
            b1_gate_rows,
            f"run.B1.{candidate_id}",
            f"2026-08-11T00:00:{index + 1:02d}Z",
            status="failed" if index in {1, 4} else "completed",
        )
    complete(b1_gate_rows, "run.B2.full", "2026-08-11T00:00:10Z")
    candidate_module._materialize_candidate_campaign_ineligibility(
        tasks=plan["tasks"],
        statuses_by_id=b1_gate_rows,
        loaded_studies={},
        promoted=set(),
    )
    assert ready_rows(b1_gate_rows) == [f"run.B2.{candidate_ids[0]}"]
    complete(
        b1_gate_rows,
        f"run.B2.{candidate_ids[0]}",
        "2026-08-11T00:01:00Z",
    )
    candidate_module._materialize_candidate_campaign_ineligibility(
        tasks=plan["tasks"],
        statuses_by_id=b1_gate_rows,
        loaded_studies={},
        promoted=set(),
    )
    assert b1_gate_rows[f"run.B2.{candidate_ids[1]}"]["completed_at"] == (
        "2026-08-11T00:01:00Z"
    )
    assert ready_rows(b1_gate_rows) == [f"run.B2.{candidate_ids[2]}"]
    for index in (2, 3):
        complete(
            b1_gate_rows,
            f"run.B2.{candidate_ids[index]}",
            f"2026-08-11T00:01:0{index}Z",
        )
        candidate_module._materialize_candidate_campaign_ineligibility(
            tasks=plan["tasks"],
            statuses_by_id=b1_gate_rows,
            loaded_studies={},
            promoted=set(),
        )
    assert b1_gate_rows[f"run.B2.{candidate_ids[4]}"]["completed_at"] == (
        "2026-08-11T00:01:03Z"
    )
    assert ready_rows(b1_gate_rows) == [f"run.B2.{candidate_ids[5]}"]

    b3_gate_rows = pending_rows()
    b4_full_index = ordered_task_ids.index("run.B4.full")
    for task_id in ordered_task_ids[: b4_full_index + 1]:
        complete(b3_gate_rows, task_id, "2026-08-11T00:02:00Z")
    promoted = {
        candidate_id
        for index, candidate_id in enumerate(candidate_ids)
        if index not in {1, 4}
    }
    loaded_studies = {"B3": {"generated_at": "2026-08-11T00:01:30Z"}}
    candidate_module._materialize_candidate_campaign_ineligibility(
        tasks=plan["tasks"],
        statuses_by_id=b3_gate_rows,
        loaded_studies=loaded_studies,
        promoted=promoted,
    )
    assert ready_rows(b3_gate_rows) == [f"run.B4.{candidate_ids[0]}"]
    complete(
        b3_gate_rows,
        f"run.B4.{candidate_ids[0]}",
        "2026-08-11T00:03:00Z",
    )
    candidate_module._materialize_candidate_campaign_ineligibility(
        tasks=plan["tasks"],
        statuses_by_id=b3_gate_rows,
        loaded_studies=loaded_studies,
        promoted=promoted,
    )
    assert b3_gate_rows[f"run.B4.{candidate_ids[1]}"]["completed_at"] == (
        "2026-08-11T00:03:00Z"
    )
    assert ready_rows(b3_gate_rows) == [f"run.B4.{candidate_ids[2]}"]


def test_candidate_campaign_status_dispatch_preserves_optional_none_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _input_files(tmp_path, "plan.json", "run.json", "study.json")
    received: dict[str, Any] = {}

    def fake_update_candidate_campaign_status(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-campaign-status.v1",
            "state": "active",
        }

    monkeypatch.setattr(
        cli, "update_candidate_campaign_status", fake_update_candidate_campaign_status
    )
    monkeypatch.setattr(
        cli,
        "build_candidate_raw_evidence_registry",
        lambda **_: {"schema_version": "candidate-raw-evidence.v1"},
    )
    args = cli.build_parser().parse_args(
        [
            "candidates",
            "campaign-status",
            "--plan",
            "plan.json",
            "--workspace-root",
            str(tmp_path),
            "--run",
            "run.B1.full=run.json",
            "--raw-evidence-registry",
            "raw-registry.json",
            "--study",
            "B2=study.json",
            "--started-at",
            "2026-08-11T00:00:00Z",
            "--as-of",
            "2026-08-11T00:01:00Z",
            "--output",
            "status.json",
        ]
    )

    assert cli.dispatch(args) == 0
    assert received["run_paths"] == {"run.B1.full": tmp_path / "run.json"}
    assert received["study_paths"] == {"B2": tmp_path / "study.json"}
    assert received["raw_evidence_registry_path"] == tmp_path / "raw-registry.json"
    assert received["freeze_path"] is None
    assert received["diagnostic_matrix_path"] is None
    assert received["final_path"] is None
    assert received["previous_status_path"] is None
    assert received["status_ledger_paths"] == []
    assert (tmp_path / "status.json").is_file()


def test_candidate_status_and_raw_snapshot_outputs_are_create_only(
    tmp_path: Path,
) -> None:
    output = cli._workspace_immutable_output_path(
        tmp_path, Path("status.json"), label="candidate status"
    )
    first = {"schema_version": "test.v1", "value": 1}
    cli._publish_immutable_json(output, first, label="candidate status")
    cli._publish_immutable_json(output, first, label="candidate status")
    with pytest.raises(ConfigurationError, match="different bytes"):
        cli._publish_immutable_json(
            output,
            {"schema_version": "test.v1", "value": 2},
            label="candidate status",
        )


@pytest.mark.parametrize("spelling", ["analyze", "study"])
def test_candidate_analyze_dispatch_passes_only_study_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    _input_files(
        tmp_path,
        "catalog.json",
        "pass-registry.json",
        "matrix.json",
        "campaign-plan.json",
        "campaign-status.json",
        "baseline.json",
        "candidate.json",
    )
    (tmp_path / "raw-state").mkdir()
    received: dict[str, Any] = {}

    def fake_build_candidate_study(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-study.v1",
            "candidates": [{"eligible_for_ranking": True}],
        }

    monkeypatch.setattr(cli, "build_candidate_study", fake_build_candidate_study)
    args = cli.build_parser().parse_args(
        [
            "candidates",
            spelling,
            "--registry",
            "catalog.json",
            "--pass-registry",
            "pass-registry.json",
            "--matrix",
            "matrix.json",
            "--workspace-root",
            str(tmp_path),
            "--raw-state-root",
            "raw-state",
            "baseline.json",
            "--candidate",
            "candidate.alpha=candidate.json",
            "--study-id",
            "study-b3",
            "--title",
            "B3",
            "--output",
            "study.json",
        ]
    )

    assert cli.dispatch(args) == 0
    assert set(received) == {
        "catalog_path",
        "pass_registry_path",
        "matrix_path",
        "workspace_root",
        "raw_state_root",
        "baseline_path",
        "candidate_paths",
        "study_id",
        "title",
        "bootstrap_samples",
        "seed",
        "interaction_paths",
    }
    assert received["raw_state_root"] == tmp_path / "raw-state"
    assert received["candidate_paths"] == {
        "candidate.alpha": tmp_path / "candidate.json"
    }
    assert received["interaction_paths"] == {}
    assert json.loads((tmp_path / "study.json").read_text(encoding="utf-8"))[
        "schema_version"
    ] == "candidate-study.v1"


def test_candidate_final_dispatch_binds_b1_and_b2_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _input_files(
        tmp_path,
        "screening.json",
        "catalog.json",
        "matrix.json",
        "campaign-plan.json",
        "campaign-status.json",
        "b1.json",
        "b2.json",
        "b3.json",
        "b4.json",
        "b5.json",
        "b6.json",
        "freeze.json",
    )
    received: dict[str, Any] = {}

    def fake_build_candidate_final(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-final.v1",
            "ranking": [],
        }

    monkeypatch.setattr(cli, "build_candidate_final", fake_build_candidate_final)
    args = cli.build_parser().parse_args(
        [
            "candidates",
            "final",
            "--workspace-root",
            str(tmp_path),
            "--screening",
            "screening.json",
            "--registry",
            "catalog.json",
            "--matrix",
            "matrix.json",
            "--campaign-plan",
            "campaign-plan.json",
            "--campaign-status",
            "campaign-status.json",
            "--status-ledger",
            "campaign-status.json",
            "--run",
            "run.B1.full=b1.json",
            "--b2-study",
            "b2.json",
            "--study",
            "B3=b3.json",
            "--study",
            "B4=b4.json",
            "--study",
            "B5=b5.json",
            "--study",
            "B6=b6.json",
            "--final-id",
            "final-1",
            "--freeze",
            "freeze.json",
            "--output",
            "final.json",
        ]
    )

    assert cli.dispatch(args) == 0
    assert received["run_paths"] == {
        "run.B1.full": tmp_path / "b1.json"
    }
    assert received["b2_study_path"] == tmp_path / "b2.json"
    assert received["freeze_path"] == tmp_path / "freeze.json"
    assert received["study_paths"] == {
        role: tmp_path / f"{role.lower()}.json"
        for role in ("B3", "B4", "B5", "B6")
    }


def test_candidate_campaign_finalize_dispatch_builds_pre_b3_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [
        "plan.json",
        "status.json",
        "study.json",
        "catalog.json",
        "pass-registry.json",
        "matrix.json",
        "screening.json",
        "oracle.json",
        "standard.json",
        "hotblock.json",
        "toolchain.json",
        "compiler.jar",
        *(f"{role.lower()}.json" for role in ("B1", "B2", "B3", "B4", "B5", "B6")),
    ]
    _input_files(tmp_path, *names)
    received: dict[str, Any] = {}

    def fake_finalize_candidate_campaign(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {
            "schema_version": "candidate-freeze.v1",
            "frozen_candidate_ids": ["candidate.alpha"],
        }

    monkeypatch.setattr(
        cli, "finalize_candidate_campaign", fake_finalize_candidate_campaign
    )
    arguments = [
        "candidates",
        "campaign-finalize",
        "--plan",
        "plan.json",
        "--workspace-root",
        str(tmp_path),
        "--status",
        "status.json",
        "--status-ledger",
        "status.json",
        "--study",
        "study.json",
        "--registry",
        "catalog.json",
        "--pass-registry",
        "pass-registry.json",
        "--matrix",
        "matrix.json",
        "--screening",
        "screening.json",
        "--oracle",
        "oracle.json",
    ]
    for role in ("B1", "B2", "B3", "B4", "B5", "B6"):
        arguments.extend(["--manifest", f"{role}={role.lower()}.json"])
    arguments.extend(
        [
            "--measurement-protocol",
            "standard.json",
            "--hotblock-measurement-protocol",
            "hotblock.json",
            "--reference-toolchain",
            "toolchain.json",
            "--compiler-artifact",
            "compiler.jar",
            "--freeze-id",
            "freeze-1",
            "--output",
            "freeze.json",
        ]
    )

    assert cli.dispatch(cli.build_parser().parse_args(arguments)) == 0
    assert received["suite_paths"] == {
        role: tmp_path / f"{role.lower()}.json"
        for role in ("B1", "B2", "B3", "B4", "B5", "B6")
    }
    assert received["freeze_id"] == "freeze-1"
    assert json.loads((tmp_path / "freeze.json").read_text(encoding="utf-8"))[
        "schema_version"
    ] == "candidate-freeze.v1"


def test_candidate_cli_rejects_duplicate_assignments_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _input_files(
        tmp_path,
        "catalog.json",
        "pass-registry.json",
        "matrix.json",
        "baseline.json",
        "candidate.json",
    )
    (tmp_path / "raw-state").mkdir()
    monkeypatch.setattr(
        cli,
        "build_candidate_study",
        lambda **_: pytest.fail("duplicate assignments must fail before analysis"),
    )
    args = cli.build_parser().parse_args(
        [
            "candidates",
            "analyze",
            "--registry",
            "catalog.json",
            "--pass-registry",
            "pass-registry.json",
            "--matrix",
            "matrix.json",
            "--workspace-root",
            str(tmp_path),
            "--raw-state-root",
            "raw-state",
            "baseline.json",
            "--candidate",
            "candidate.alpha=candidate.json",
            "--candidate",
            "candidate.alpha=candidate.json",
            "--study-id",
            "study-b3",
            "--title",
            "B3",
            "--output",
            "study.json",
        ]
    )

    with pytest.raises(ConfigurationError, match="duplicate candidate run"):
        cli.dispatch(args)


def test_screening_spec_enforces_locked_family_structure_contract() -> None:
    _, valid, _ = _screening_documents()
    assert validate_document(valid) == valid

    invalid_eligible = json.loads(json.dumps(valid))
    invalid_eligible["candidates"][1]["eligible_oracle_structure_refs"] = []
    with pytest.raises(ValidationError, match="locked eleven-family structure"):
        validate_document(invalid_eligible)

    invalid_blocked = json.loads(json.dumps(valid))
    invalid_blocked["candidates"][0].update(
        structural_disposition="eligible",
        structural_reason=None,
        eligible_oracle_structure_refs=[
            {"oracle_family_id": "bitset", "structure_id": "scalar_bitset"}
        ],
    )
    with pytest.raises(ValidationError, match="locked eleven-family structure"):
        validate_document(invalid_blocked)


def test_shared_campaign_status_primitives_enforce_chain_terminal_and_readiness() -> None:
    start, observed, previous_digest = campaign_status_chain(
        campaign_id="campaign-1",
        plan_sha256="a" * 64,
        previous=None,
        started_at="2026-08-11T00:00:00+00:00",
        as_of="2026-08-11T00:01:00+00:00",
    )
    assert start == "2026-08-11T00:00:00.000Z"
    assert observed == "2026-08-11T00:01:00.000Z"
    assert previous_digest is None

    tasks = [
        {"task_id": "run.full", "dependencies": []},
        {"task_id": "run.candidate", "dependencies": ["run.full"]},
    ]
    pending = [
        {
            "task_id": "run.full",
            "status": "pending",
            "started_at": None,
            "completed_at": None,
        },
        {
            "task_id": "run.candidate",
            "status": "pending",
            "started_at": None,
            "completed_at": None,
        },
    ]
    assert ready_campaign_task_ids(tasks=tasks, statuses=pending) == ["run.full"]
    completed = json.loads(json.dumps(pending))
    completed[0].update(
        status="completed",
        started_at="2026-08-11T00:01:00.000Z",
        completed_at="2026-08-11T00:02:00.000Z",
    )
    assert ready_campaign_task_ids(tasks=tasks, statuses=completed) == [
        "run.candidate"
    ]

    mutated = json.loads(json.dumps(completed))
    mutated[0]["completed_at"] = "2026-08-11T00:03:00.000Z"
    with pytest.raises(ValidationError, match="terminal candidate campaign task"):
        enforce_terminal_task_immutability(
            previous_rows=completed,
            current_rows=mutated,
            label="candidate campaign",
        )


def _captured_structure(
    *, family: str, structure: str, speedup: float
) -> dict[str, Any]:
    sizes: dict[str, Any] = {}
    for size in ("small", "medium", "large"):
        sizes[size] = {
            "pair_id": f"{family}:{structure}:{size}",
            "family": family,
            "structure_id": structure,
            "size": size,
            "baseline_case_id": f"baseline:{family}:{structure}:{size}",
            "optimized_case_id": f"optimized:{family}:{structure}:{size}",
            "eligible_for_ranking": True,
            "ineligibility_reason": None,
            "baseline_metric_value": 200.0,
            "optimized_metric_value": 200.0 / speedup,
            "speedup": speedup,
        }
    return {
        "structure_id": structure,
        "sizes": sizes,
        "paired_datasets": 3,
        "eligible_for_ranking": True,
        "ineligibility_reason": None,
        "geometric_mean_upper_bound": speedup,
    }


def _screening_documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    contracts = [
        (
            "bitset",
            "bitset",
            "blocked",
            "blocked_locked_bitset_capability_gap",
            [],
            ["scalar_bitset", "vector_bitset", "word_bitset"],
        ),
        (
            "boom_ilp",
            "standard",
            "eligible",
            None,
            ["dot_unroll4", "reduction_multi_acc"],
            ["dot_unroll4", "independent_chains", "reduction_multi_acc"],
        ),
        (
            "closed_form",
            "standard",
            "eligible",
            None,
            ["linear_sum", "quadratic_sum", "triangular_recurrence"],
            ["linear_sum", "quadratic_sum", "triangular_recurrence"],
        ),
        (
            "dp_storage",
            "standard",
            "eligible",
            None,
            ["reverse_single_row", "three_row", "two_row"],
            ["reverse_single_row", "three_row", "two_row"],
        ),
        (
            "finite_state",
            "standard",
            "eligible",
            None,
            ["affine_mod97", "branch_mod53", "quadratic_mod31"],
            ["affine_mod97", "branch_mod53", "quadratic_mod31"],
        ),
        (
            "fusion",
            "standard",
            "eligible",
            None,
            ["single_temporary", "stencil_producer", "two_temporaries"],
            ["single_temporary", "stencil_producer", "two_temporaries"],
        ),
        (
            "linear_transition",
            "standard",
            "eligible",
            None,
            ["affine_2d", "affine_scalar", "fibonacci_2d"],
            ["affine_2d", "affine_scalar", "fibonacci_2d"],
        ),
        (
            "memoization",
            "standard",
            "eligible",
            None,
            ["binomial", "grid_paths"],
            ["fibonacci", "binomial", "grid_paths"],
        ),
        (
            "prefix_scan",
            "standard",
            "eligible",
            None,
            ["forward_prefix", "reverse_suffix", "weighted_prefix"],
            ["forward_prefix", "reverse_suffix", "weighted_prefix"],
        ),
        (
            "recursion_worklist",
            "mixed",
            "rejected",
            "mixed_family_no_unified_transform",
            [],
            ["tail", "divide_conquer", "dfs"],
        ),
        (
            "structured_kernel",
            "mixed",
            "rejected",
            "mixed_family_no_unified_transform",
            [],
            ["gemm", "stencil", "transpose"],
        ),
    ]
    evidence_candidates: list[dict[str, Any]] = []
    spec_candidates: list[dict[str, Any]] = []
    captured_candidates: list[dict[str, Any]] = []
    for (
        candidate_id,
        family_kind,
        disposition,
        structural_reason,
        allowed,
        structure_ids,
    ) in contracts:
        family = candidate_id
        implementation_id = (
            f"candidate.{candidate_id}" if disposition == "eligible" else None
        )
        evidence_candidates.append(
            {
                "candidate_id": candidate_id,
                "cleanroom_oracle_family_id": family,
                "legality_proof_path": "clear",
                "legality_obligation_ids": [
                    f"{implementation_id or candidate_id}.legality"
                ],
                "implementation_cost": "medium",
                "risk": "medium",
                "specification_status": "clear",
                "requires_boom_feature": False,
            }
        )
        eligible_refs = [
            {"oracle_family_id": family, "structure_id": structure}
            for structure in allowed
        ]
        if candidate_id == "fusion":
            eligible_refs.append(
                {
                    "oracle_family_id": "boom_ilp",
                    "structure_id": "independent_chains",
                }
            )
        spec_candidates.append(
            {
                "candidate_id": candidate_id,
                "oracle_family_id": family,
                "implementation_candidate_id": implementation_id,
                "family_kind": family_kind,
                "overlaps_existing_pass_ids": [],
                "duplicate_of": None,
                "eligible_oracle_structure_refs": eligible_refs,
                "structural_disposition": disposition,
                "structural_reason": structural_reason,
            }
        )
        captured_candidates.append(
            {
                "candidate_id": candidate_id,
                "oracle_family_id": family,
                "structures": [
                    _captured_structure(
                        family=family,
                        structure=structure,
                        speedup=(
                            1.0
                            if candidate_id == "boom_ilp"
                            and structure in set(allowed)
                            else 2.0
                        ),
                    )
                    for structure in structure_ids
                ],
            }
        )
    evidence = {
        "schema_version": "candidate-evidence.v1",
        "snapshot_id": "candidate-evidence-1",
        "candidates": evidence_candidates,
    }
    pass_registry = {
        "schema_version": "pass-registry.v2",
        "passes": [],
    }
    spec = {
        "schema_version": "candidate-screening-spec.v1",
        "spec_id": "candidate-screening-spec-1",
        "pass_registry_sha256": candidate_module.sha256_json(pass_registry),
        "candidates": spec_candidates,
    }
    capture = {
        "schema_version": "candidate-oracle-capture.v1",
        "generated_at": "2026-08-11T00:00:00Z",
        "candidate_evidence_sha256": candidate_module.sha256_json(evidence),
        "candidates": captured_candidates,
    }
    return evidence, spec, capture


def _patch_screening_inputs(
    monkeypatch: pytest.MonkeyPatch,
    evidence: dict[str, Any],
    spec: dict[str, Any],
    capture: dict[str, Any],
) -> None:
    documents = {
        "evidence.json": evidence,
        "spec.json": spec,
        "capture.json": capture,
        "registry.json": {
            "schema_version": "pass-registry.v2",
            "passes": [],
        },
    }
    monkeypatch.setattr(
        candidate_module,
        "_load_version",
        lambda path, version, *, label: documents[path.name],
    )
    monkeypatch.setattr(
        candidate_module,
        "_load_and_reverify_candidate_oracle_capture",
        lambda **_: capture,
    )
    monkeypatch.setattr(
        candidate_module,
        "_frozen_artifact_digest",
        lambda workspace_root, path, document, *, label: {
            "path": path.as_posix(),
            "canonical_sha256": sha256_json(document),
            "physical_sha256": "f" * 64,
        },
    )


def test_screening_excluded_structure_cannot_qualify_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, spec, capture = _screening_documents()
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)

    result = candidate_module.build_candidate_screening(
        candidate_evidence_path=Path("evidence.json"),
        screening_spec_path=Path("spec.json"),
        pass_registry_path=Path("registry.json"),
        oracle_capture_path=Path("capture.json"),
        workspace_root=Path("."),
        screening_id="screening-1",
    )

    boom = next(item for item in result["candidates"] if item["candidate_id"] == "boom_ilp")
    independent = next(
        item
        for item in boom["oracle_structures"]
        if item["structure_id"] == "independent_chains"
    )
    assert independent["geometric_mean_speedup"] == 2.0
    assert independent["eligible_for_candidate_screening"] is False
    assert independent["meets_threshold"] is False
    assert boom["qualification_status"] == "blocked"
    assert boom["qualifying_oracle_structure_refs"] == []
    assert boom["rejection_reasons"] == ["oracle_structure_below_1_10"]
    bitset = next(item for item in result["candidates"] if item["candidate_id"] == "bitset")
    assert bitset["rejection_reasons"] == [
        "blocked_locked_bitset_capability_gap"
    ]
    assert validate_document(result) == result
    repeated = candidate_module.build_candidate_screening(
        candidate_evidence_path=Path("evidence.json"),
        screening_spec_path=Path("spec.json"),
        pass_registry_path=Path("registry.json"),
        oracle_capture_path=Path("capture.json"),
        workspace_root=Path("."),
        screening_id="screening-1",
    )
    assert sha256_json(repeated) == sha256_json(result)


def test_screening_incomplete_allowed_structures_ignore_complete_excluded_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, spec, capture = _screening_documents()
    memo = next(item for item in capture["candidates"] if item["candidate_id"] == "memoization")
    for structure in memo["structures"]:
        if structure["structure_id"] == "fibonacci":
            continue
        structure["eligible_for_ranking"] = False
        structure["ineligibility_reason"] = "incomplete_or_incorrect_oracle"
        structure["geometric_mean_upper_bound"] = None
        for pair in structure["sizes"].values():
            pair["eligible_for_ranking"] = False
            pair["ineligibility_reason"] = "correctness_failure"
            pair["speedup"] = None
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)

    result = candidate_module.build_candidate_screening(
        candidate_evidence_path=Path("evidence.json"),
        screening_spec_path=Path("spec.json"),
        pass_registry_path=Path("registry.json"),
        oracle_capture_path=Path("capture.json"),
        workspace_root=Path("."),
        screening_id="screening-1",
    )

    screened = next(
        item for item in result["candidates"] if item["candidate_id"] == "memoization"
    )
    assert screened["qualification_status"] == "blocked"
    assert screened["rejection_reasons"] == ["no_complete_oracle_structure"]


def test_screening_fusion_qualifies_from_fully_qualified_cross_family_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, spec, capture = _screening_documents()
    fusion_capture = next(
        item for item in capture["candidates"] if item["candidate_id"] == "fusion"
    )
    for structure in fusion_capture["structures"]:
        structure["geometric_mean_upper_bound"] = 1.0
        for pair in structure["sizes"].values():
            pair["optimized_metric_value"] = pair["baseline_metric_value"]
            pair["speedup"] = 1.0
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)

    result = candidate_module.build_candidate_screening(
        candidate_evidence_path=Path("evidence.json"),
        screening_spec_path=Path("spec.json"),
        pass_registry_path=Path("registry.json"),
        oracle_capture_path=Path("capture.json"),
        workspace_root=Path("."),
        screening_id="screening-1",
    )

    fusion = next(item for item in result["candidates"] if item["candidate_id"] == "fusion")
    cross_ref = {
        "oracle_family_id": "boom_ilp",
        "structure_id": "independent_chains",
    }
    assert fusion["qualification_status"] == "qualified"
    assert fusion["qualifying_oracle_structure_refs"] == [cross_ref]
    assert fusion["eligible_oracle_structure_refs"][-1] == cross_ref
    assert [
        (item["oracle_family_id"], item["structure_id"])
        for item in fusion["oracle_structures"]
    ] == [
        ("fusion", "single_temporary"),
        ("fusion", "stencil_producer"),
        ("fusion", "two_temporaries"),
        ("boom_ilp", "independent_chains"),
    ]


def test_screening_replay_rejects_coherent_qualification_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, spec, capture = _screening_documents()
    pass_registry = {"schema_version": "pass-registry.v2", "passes": []}
    documents = {
        "evidence.json": evidence,
        "spec.json": spec,
        "capture.json": capture,
        "registry.json": pass_registry,
    }
    for name in documents:
        (tmp_path / name).write_text("{}\n", encoding="utf-8")

    def frozen_digest(
        workspace_root: Path,
        path: Path,
        document: dict[str, Any],
        *,
        label: str,
    ) -> dict[str, str]:
        del label
        relative = path.resolve().relative_to(workspace_root.resolve())
        return {
            "path": relative.as_posix(),
            "canonical_sha256": sha256_json(document),
            "physical_sha256": "f" * 64,
        }

    monkeypatch.setattr(
        candidate_module,
        "_load_version",
        lambda path, version, *, label: deepcopy(documents[path.name]),
    )
    monkeypatch.setattr(
        candidate_module,
        "_load_and_reverify_candidate_oracle_capture",
        lambda **_: deepcopy(capture),
    )
    monkeypatch.setattr(candidate_module, "_frozen_artifact_digest", frozen_digest)
    screening = candidate_module.build_candidate_screening(
        candidate_evidence_path=tmp_path / "evidence.json",
        screening_spec_path=tmp_path / "spec.json",
        pass_registry_path=tmp_path / "registry.json",
        oracle_capture_path=tmp_path / "capture.json",
        workspace_root=tmp_path,
        screening_id="screening-1",
    )
    forged = deepcopy(screening)
    boom = next(item for item in forged["candidates"] if item["candidate_id"] == "boom_ilp")
    for structure in boom["oracle_structures"]:
        if not structure["eligible_for_candidate_screening"]:
            continue
        structure["geometric_mean_speedup"] = 2.0
        structure["meets_threshold"] = True
        for pair in structure["sizes"].values():
            pair["optimized_metric_value"] = pair["baseline_metric_value"] / 2.0
            pair["speedup"] = 2.0
    boom["qualifying_oracle_structure_refs"] = deepcopy(
        boom["eligible_oracle_structure_refs"]
    )
    boom["qualification_status"] = "qualified"
    boom["rejection_reasons"] = []
    assert validate_document(forged) == forged
    documents["screening.json"] = forged
    (tmp_path / "screening.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="exact source/capture replay"):
        candidate_module._load_and_reverify_candidate_screening(
            screening_path=tmp_path / "screening.json",
            workspace_root=tmp_path,
        )


def test_candidate_screen_dispatch_writes_deterministic_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, spec, capture = _screening_documents()
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)
    screening = candidate_module.build_candidate_screening(
        candidate_evidence_path=Path("evidence.json"),
        screening_spec_path=Path("spec.json"),
        pass_registry_path=Path("registry.json"),
        oracle_capture_path=Path("capture.json"),
        workspace_root=Path("."),
        screening_id="screening-1",
    )
    _input_files(tmp_path, "evidence.json", "spec.json", "capture.json", "registry.json")
    monkeypatch.setattr(cli, "build_candidate_screening", lambda **_: screening)
    arguments = cli.build_parser().parse_args(
        [
            "candidates",
            "screen",
            "--workspace-root",
            str(tmp_path),
            "--evidence",
            "evidence.json",
            "--spec",
            "spec.json",
            "--pass-registry",
            "registry.json",
            "--oracle",
            "capture.json",
            "--screening-id",
            "screening-1",
            "--output",
            "screening.json",
            "--report",
            "report-a",
        ]
    )

    assert cli.dispatch(arguments) == 0
    first = tmp_path / "report-a/CANDIDATE_SCREENING_REPORT.zh-CN.md"
    second = build_candidate_screening_report(
        screening=screening,
        output_directory=tmp_path / "report-b",
    )["CANDIDATE_SCREENING_REPORT.zh-CN.md"]
    assert sha256_file(first) == sha256_file(second)
    assert "Oracle 上界声明" in first.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "eligible_refs, message",
    [
        (
            [{"oracle_family_id": "boom_ilp", "structure_id": "unknown"}],
            "unknown Oracle structures",
        ),
        (
            [
                {
                    "oracle_family_id": "boom_ilp",
                    "structure_id": "reduction_multi_acc",
                },
                {
                    "oracle_family_id": "boom_ilp",
                    "structure_id": "dot_unroll4",
                },
            ],
            "not an ordered capture subset",
        ),
    ],
)
def test_screening_rejects_unknown_or_reordered_structure_contract(
    monkeypatch: pytest.MonkeyPatch,
    eligible_refs: list[dict[str, str]],
    message: str,
) -> None:
    evidence, spec, capture = _screening_documents()
    spec["candidates"][1]["eligible_oracle_structure_refs"] = eligible_refs
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)

    with pytest.raises(ValidationError, match=message):
        candidate_module.build_candidate_screening(
            candidate_evidence_path=Path("evidence.json"),
            screening_spec_path=Path("spec.json"),
            pass_registry_path=Path("registry.json"),
            oracle_capture_path=Path("capture.json"),
            workspace_root=Path("."),
            screening_id="screening-1",
        )


def _candidate_per_cases(speedup: float, *, count: int = 60) -> list[dict[str, Any]]:
    return [
        {
            "case_id": f"case-{index:03d}",
            "source_group": f"source-{index:03d}",
            "family": "family",
            "target": "rv64gc",
            "weight": 1.0,
            "metric_full": 100.0,
            "metric_full_plus_candidate": 100.0 / speedup,
            "speedup": speedup,
        }
        for index in range(count)
    ]


def _raw_run_ref(run_id: str, canonical_sha256: str, index: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run_canonical_sha256": canonical_sha256,
        "run_physical_sha256": f"{index + 100:064x}",
        "terminal_observed_at": "2026-08-11T00:00:00Z",
        "terminal_journal_sha256": f"{index + 150:064x}",
        "terminal_journal_event_count": 1,
        "state_tree_sha256": f"{index + 200:064x}",
        "raw_evidence_sha256": f"{index + 300:064x}",
        "attempt_count": 60,
        "terminal_attempt_count": 60,
    }


def _candidate_study_result(candidate_id: str, speedup: float, index: int) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "logical_profile_id": f"full+{candidate_id}",
        "run_id": f"run-{candidate_id}",
        "run_sha256": f"{index + 1:064x}",
        "configuration_sha256": f"{index + 11:064x}",
        "enabled_candidate_ids": [candidate_id],
        "comparable_cases": 60,
        "comparable_source_groups": 60,
        "correctness_failures": 0,
        "censored_cases": 0,
        "excluded_cases": 0,
        "eligible_for_ranking": True,
        "ineligibility_reason": None,
        "case_geometric_mean_speedup": speedup,
        "source_group_geometric_mean_speedup": speedup,
        "confidence_interval_95": None,
        "static_text_bytes_full": 6000.0,
        "static_text_bytes_full_plus_candidate": 5900.0,
        "static_text_ratio": 6000.0 / 5900.0,
        "remarks": {
            "case_count": 60,
            "paired_candidate_count": 2,
            "applied_count": 1,
            "rejected_count": 1,
            "legality_obligation_ids": [f"obligation.{candidate_id}"],
        },
        "per_cases": _candidate_per_cases(speedup),
        "families": [],
    }


def test_candidate_interaction_formula_and_top3_coverage_are_semantic() -> None:
    singles = [
        _candidate_study_result("candidate.a", 1.2, 0),
        _candidate_study_result("candidate.b", 1.1, 1),
        _candidate_study_result("candidate.c", 1.05, 2),
    ]
    interactions: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(
        (("candidate.a", "candidate.b"), ("candidate.a", "candidate.c"), ("candidate.b", "candidate.c"))
    ):
        left_speedup = next(
            item["case_geometric_mean_speedup"]
            for item in singles
            if item["candidate_id"] == left
        )
        right_speedup = next(
            item["case_geometric_mean_speedup"]
            for item in singles
            if item["candidate_id"] == right
        )
        delta = 0.01 * (index + 1)
        pair_speedup = left_speedup * right_speedup * candidate_module.math.exp(delta)
        interactions.append(
            {
                "candidate_ids": [left, right],
                "logical_profile_id": f"full+{left}+{right}",
                "run_id": f"run-pair-{index}",
                "run_sha256": f"{index + 21:064x}",
                "configuration_sha256": f"{index + 31:064x}",
                "enabled_candidate_ids": [left, right],
                "comparable_cases": 60,
                "correctness_failures": 0,
                "censored_cases": 0,
                "excluded_cases": 0,
                "candidate_observations": [
                    {"candidate_id": left, "paired_candidate_count": 1},
                    {"candidate_id": right, "paired_candidate_count": 1},
                ],
                "eligible_for_ranking": True,
                "ineligibility_reason": None,
                "pair_case_geometric_mean_speedup": pair_speedup,
                "expected_multiplicative_speedup": left_speedup * right_speedup,
                "delta_ln_geometric_mean": delta,
                "per_cases": _candidate_per_cases(pair_speedup),
            }
        )
    document = {
        "schema_version": "candidate-study.v1",
        "study_id": "study-b3",
        "title": "B3 candidates and Top3 interactions",
        "generated_at": "2026-08-11T00:00:00Z",
        "matrix_sha256": "a" * 64,
        "candidate_registry_sha256": "b" * 64,
        "pass_registry_sha256": "c" * 64,
        "suite_id": "suite-b3",
        "data_role": "B3",
        "manifest_sha256": "d" * 64,
        "bindings": {
            "repo_commit": "e" * 40,
            "repo_dirty": False,
            "tracked_diff_sha256": None,
            "compiler_artifact_sha256": "f" * 64,
            "measurement_protocol_id": "protocol",
            "measurement_protocol_sha256": "1" * 64,
            "pipeline_profile_id": "candidate-empty",
            "pipeline_profile_sha256": "2" * 64,
        },
        "baseline": {
            "run_id": "run-full",
            "run_sha256": "3" * 64,
            "configuration_sha256": "4" * 64,
        },
        "raw_evidence": {
            "baseline": _raw_run_ref("run-full", "3" * 64, 0),
            "candidates": [
                {
                    "candidate_id": item["candidate_id"],
                    "run": _raw_run_ref(
                        item["run_id"], item["run_sha256"], index + 1
                    ),
                }
                for index, item in enumerate(singles)
            ],
            "interactions": [
                {
                    "candidate_ids": item["candidate_ids"],
                    "run": _raw_run_ref(
                        item["run_id"], item["run_sha256"], index + 10
                    ),
                }
                for index, item in enumerate(interactions)
            ],
        },
        "primary_metric_id": "dynamic_instruction_count",
        "metric_unit": "instructions",
        "bootstrap_samples": 10_000,
        "seed": 20260809,
        "candidates": singles,
        "interactions": interactions,
    }

    assert validate_document(document) == document
    tampered = json.loads(json.dumps(document))
    tampered["interactions"][0]["delta_ln_geometric_mean"] += 0.1
    with pytest.raises(ValidationError, match=r"ln\(S_AB\)-ln\(S_A\)-ln\(S_B\)"):
        validate_document(tampered)


def _remark_event(
    *,
    sequence: int,
    target: str,
    event_type: str,
    decision: str | None = None,
    changed: bool | None = None,
    target_kind: str = "function",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": "optimization-remark.v2",
        "sequence": sequence,
        "event_type": event_type,
        "pass": "candidate.test",
        "occurrence": 1,
        "stage": "ir_function",
        "target_kind": target_kind,
        "target_name": target,
    }
    if event_type == "pass_summary":
        event.update(
            elapsed_ns=1,
            changed=changed,
            before={},
            after={},
            delta={},
            details={},
            decision_observability="available",
        )
    else:
        assert decision is not None
        event.update(
            decision=decision,
            reason=(
                "candidate_matched"
                if decision == "candidate"
                else "applied_profitable"
                if decision == "applied"
                else "rejected_profitability"
            ),
            legality_obligation_id=None,
        )
    return event


def test_candidate_remarks_aggregate_multiple_function_summaries(
    tmp_path: Path,
) -> None:
    pass_registry = {
        "schema_version": "pass-registry.v2",
        "passes": [
            {
                "id": "candidate.test",
                "lifecycle": "candidate",
            }
        ],
    }
    catalog = {
        "schema_version": "candidate-catalog.v1",
        "pass_registry_sha256": sha256_json(pass_registry),
        "candidates": [
            {
                "candidate_id": "candidate.test",
                "stage": "ir_function",
                "legality_obligations": [
                    {"obligation_id": "candidate.test.legality"}
                ],
            }
        ],
    }
    long_function_target = "@" + "f" * 2048
    events = [
        _remark_event(
            sequence=1,
            target=long_function_target + ":header",
            target_kind="loop",
            event_type="decision",
            decision="candidate",
        ),
        _remark_event(
            sequence=2,
            target=long_function_target + ":header",
            target_kind="loop",
            event_type="decision",
            decision="applied",
        ),
        _remark_event(
            sequence=3,
            target=long_function_target,
            event_type="pass_summary",
            changed=True,
        ),
        _remark_event(
            sequence=4,
            target="@bar:header",
            target_kind="loop",
            event_type="decision",
            decision="candidate",
        ),
        _remark_event(
            sequence=5,
            target="@bar:header",
            target_kind="loop",
            event_type="decision",
            decision="rejected",
        ),
        _remark_event(
            sequence=6,
            target="@bar",
            event_type="pass_summary",
            changed=False,
        ),
    ]
    path = tmp_path / "remarks.jsonl"
    path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events),
        encoding="utf-8",
    )

    summary = validate_candidate_remark_jsonl(
        path,
        catalog=catalog,
        pass_registry=pass_registry,
        enabled_candidate_ids=["candidate.test"],
        candidate_registry_sha256=sha256_json(catalog),
        pipeline_profile_id="full+candidate.test",
        pipeline_profile_sha256="a" * 64,
        require_candidate_observation=True,
    )
    assert summary["summary_count"] == 2
    assert summary["paired_candidate_count"] == 2
    assert summary["applied_count"] == 1
    assert summary["rejected_count"] == 1

    events[2]["changed"] = False
    path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="aggregate summary changed"):
        validate_candidate_remark_jsonl(
            path,
            catalog=catalog,
            pass_registry=pass_registry,
            enabled_candidate_ids=["candidate.test"],
            candidate_registry_sha256=sha256_json(catalog),
            pipeline_profile_id="full+candidate.test",
            pipeline_profile_sha256="a" * 64,
        )


def test_screening_requires_implementation_id_and_pass_scoped_obligations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, spec, capture = _screening_documents()
    spec["candidates"][1]["implementation_candidate_id"] = None
    with pytest.raises(ValidationError, match="implementation pass id"):
        validate_document(spec)

    evidence, spec, capture = _screening_documents()
    evidence["candidates"][1]["legality_obligation_ids"] = ["wrong.scope"]
    capture["candidate_evidence_sha256"] = sha256_json(evidence)
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)
    with pytest.raises(ValidationError, match="not scoped"):
        candidate_module.build_candidate_screening(
            candidate_evidence_path=Path("evidence.json"),
            screening_spec_path=Path("spec.json"),
            pass_registry_path=Path("registry.json"),
            oracle_capture_path=Path("capture.json"),
            workspace_root=Path("."),
            screening_id="screening-1",
        )

    empty = json.loads(json.dumps(evidence))
    empty["candidates"][1]["legality_obligation_ids"] = []
    with pytest.raises(ValidationError, match="non-empty"):
        validate_document(empty)


@pytest.mark.parametrize(
    ("existing_lifecycle", "expected_error"),
    (
        (None, "overlap ids must be existing non-candidate"),
        ("candidate", "zero candidates"),
    ),
)
def test_screening_rejects_unknown_or_candidate_overlap_passes(
    monkeypatch: pytest.MonkeyPatch,
    existing_lifecycle: str | None,
    expected_error: str,
) -> None:
    evidence, spec, capture = _screening_documents()
    overlap_id = "ghost" if existing_lifecycle is None else "candidate.existing"
    pass_registry = {
        "schema_version": "pass-registry.v2",
        "passes": (
            []
            if existing_lifecycle is None
            else [{"id": overlap_id, "lifecycle": existing_lifecycle}]
        ),
    }
    spec["pass_registry_sha256"] = sha256_json(pass_registry)
    spec["candidates"][1]["overlaps_existing_pass_ids"] = [overlap_id]
    documents = {
        "evidence.json": evidence,
        "spec.json": spec,
        "capture.json": capture,
        "registry.json": pass_registry,
    }
    monkeypatch.setattr(
        candidate_module,
        "_load_version",
        lambda path, version, *, label: documents[path.name],
    )
    monkeypatch.setattr(
        candidate_module,
        "_load_and_reverify_candidate_oracle_capture",
        lambda **kwargs: capture,
    )

    with pytest.raises(ValidationError, match=expected_error):
        candidate_module.build_candidate_screening(
            candidate_evidence_path=Path("evidence.json"),
            screening_spec_path=Path("spec.json"),
            pass_registry_path=Path("registry.json"),
            oracle_capture_path=Path("capture.json"),
            workspace_root=Path("."),
            screening_id="screening-1",
        )


def _registry_bridge_documents() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any]
]:
    base = {
        "schema_version": "pass-registry.v2",
        "passes": [
            {
                "id": "sccp",
                "logical_family_id": "sccp",
                "display_name": "SCCP",
                "stage": "ir_function",
                "full_pipeline_occurrences": 1,
                "lifecycle": "production",
                "decision_observable": False,
                "candidate_anchor": None,
                "legality_obligation_ids": [],
            }
        ],
    }
    candidate = {
        "id": "candidate.fusion",
        "logical_family_id": "candidate.fusion",
        "display_name": "Loop fusion",
        "stage": "ir_function",
        "full_pipeline_occurrences": 1,
        "lifecycle": "candidate",
        "decision_observable": True,
        "candidate_anchor": {
            "pass": "sccp",
            "occurrence": 1,
            "position": "after",
        },
        "legality_obligation_ids": ["candidate.fusion.alias"],
    }
    executable = {
        "schema_version": "pass-registry.v2",
        "passes": [*deepcopy(base["passes"]), candidate],
    }
    catalog = {
        "schema_version": "candidate-catalog.v1",
        "catalog_id": "catalog-1",
        "pass_registry_sha256": sha256_json(executable),
        "candidates": [
            {
                "candidate_id": "candidate.fusion",
                "display_name": "Loop fusion",
                "description": "Strict same-domain loop fusion.",
                "stage": "ir_function",
                "remark_pass_id": "candidate.fusion",
                "required_analyses": ["dependence"],
                "legality_obligations": [
                    {
                        "obligation_id": "candidate.fusion.alias",
                        "description": "Prove disjoint memory effects.",
                    }
                ],
                "oracle_family_ids": ["fusion"],
                "qualification_status": "qualified",
                "implementation_status": "implemented",
                "default_enabled": False,
            }
        ],
    }
    screening = {
        "pass_registry_sha256": sha256_json(base),
        "base_pass_registry": {
            "path": "data/pass-registry.base.v2.json",
            "canonical_sha256": sha256_json(base),
            "physical_sha256": "a" * 64,
        },
        "candidates": [
            {
                "candidate_id": "fusion",
                "implementation_candidate_id": "candidate.fusion",
                "qualification_status": "qualified",
                "legality_obligation_ids": ["candidate.fusion.alias"],
            }
        ],
    }
    return screening, catalog, executable


def test_candidate_registry_bridge_reopens_base_and_accepts_exact_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screening, catalog, executable = _registry_bridge_documents()
    base = {
        "schema_version": "pass-registry.v2",
        "passes": deepcopy(executable["passes"][:-1]),
    }
    base_path = tmp_path / "data" / "pass-registry.base.v2.json"
    base_path.parent.mkdir(parents=True)
    base_path.write_text(
        json.dumps(base, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    screening["base_pass_registry"] = {
        "path": "data/pass-registry.base.v2.json",
        "canonical_sha256": sha256_json(base),
        "physical_sha256": sha256_file(base_path),
    }
    screening["pass_registry_sha256"] = sha256_json(base)

    assert candidate_module._require_executable_registry_bridge(
        screening=screening,
        catalog=catalog,
        executable_registry=executable,
        workspace_root=tmp_path,
    ) == base

    base_path.write_text(base_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValidationError, match="physical SHA-256 differs"):
        candidate_module._require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=executable,
            workspace_root=tmp_path,
        )


@pytest.mark.parametrize(
    "tamper",
    ("base_projection", "missing_candidate", "extra_candidate", "obligation"),
)
def test_candidate_registry_bridge_rejects_identity_and_obligation_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    screening, catalog, executable = _registry_bridge_documents()
    base = {
        "schema_version": "pass-registry.v2",
        "passes": deepcopy(executable["passes"][:-1]),
    }
    monkeypatch.setattr(
        candidate_module,
        "_load_screening_base_pass_registry",
        lambda screening, *, workspace_root: deepcopy(base),
    )
    if tamper == "base_projection":
        executable["passes"][0]["display_name"] = "Changed SCCP"
    elif tamper == "missing_candidate":
        executable["passes"].pop()
    elif tamper == "extra_candidate":
        extra = deepcopy(executable["passes"][-1])
        extra["id"] = "candidate.extra"
        extra["logical_family_id"] = "candidate.extra"
        extra["legality_obligation_ids"] = ["candidate.extra.legality"]
        executable["passes"].append(extra)
    else:
        screening["candidates"][0]["legality_obligation_ids"] = [
            "candidate.fusion.bounds"
        ]
    catalog["pass_registry_sha256"] = sha256_json(executable)

    with pytest.raises(ValidationError, match="PassRegistry|registry|obligations"):
        candidate_module._require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=executable,
            workspace_root=Path("."),
        )


def test_candidate_remark_workspace_path_rejects_lexical_symlink(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "remarks.jsonl"
    physical.write_text("{}\n", encoding="utf-8")
    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(physical)

    with pytest.raises(ValidationError, match="symbolic link"):
        candidate_module._workspace_regular_path(
            tmp_path, linked, label="candidate remark candidate.a@case-1"
        )


@pytest.mark.parametrize(
    "state, accepted",
    [
        ("completed", True),
        ("failed", True),
        ("interrupted", True),
        ("pending", False),
    ],
)
def test_candidate_study_terminal_gate_accepts_immutable_interruption(
    state: str,
    accepted: bool,
) -> None:
    run = {"state": state}
    if accepted:
        candidate_module._require_analysis_terminal_run(
            run, label="candidate run candidate.a"
        )
    else:
        with pytest.raises(ValidationError, match="is not terminal"):
            candidate_module._require_analysis_terminal_run(
                run, label="candidate run candidate.a"
            )
    assert (
        candidate_module._terminal_candidate_reason(
            correctness_failures=0,
            excluded_cases=1 if state == "interrupted" else 0,
            censored_cases=0,
            comparable_cases=0 if state == "interrupted" else 1,
            paired_candidate_count=0 if state == "interrupted" else 1,
        )
        == ("incomplete_profile" if state == "interrupted" else None)
    )


def test_candidate_study_rejects_metrics_consistent_only_inside_study() -> None:
    baseline = make_run(
        "run-full",
        {
            "case-a": ("family", 100.0),
            "case-b": ("family", 200.0),
        },
        profile_id="candidate-empty",
    )
    candidate = make_run(
        "run-candidate",
        {
            "case-a": ("family", 80.0),
            "case-b": ("family", 160.0),
        },
        profile_id="full+candidate.a",
    )
    for case in candidate["cases"]:
        case["candidate_remark_summary"] = {
            "event_count": 3,
            "summary_count": 1,
            "paired_candidate_count": 1,
            "applied_count": 1,
            "rejected_count": 0,
            "candidates": [
                {
                    "candidate_id": "candidate.a",
                    "paired_candidate_count": 1,
                    "applied_count": 1,
                    "rejected_count": 0,
                }
            ],
        }
    catalog = {
        "candidates": [
            {
                "candidate_id": "candidate.a",
                "legality_obligations": [
                    {"obligation_id": "candidate.a.legality"}
                ],
            }
        ]
    }
    captured: dict[str, Any] = {}

    class CaptureExpected(dict[str, Any]):
        def __ne__(self, other: object) -> bool:
            assert isinstance(other, dict)
            captured.update(deepcopy(other))
            return False

    study: dict[str, Any] = {
        "baseline": candidate_module._run_ref(baseline),
        "raw_evidence": {
            "baseline": _raw_run_ref(
                baseline["run_id"], sha256_json(baseline), 40
            ),
            "candidates": [
                {
                    "candidate_id": "candidate.a",
                    "run": _raw_run_ref(
                        candidate["run_id"], sha256_json(candidate), 41
                    ),
                }
            ],
            "interactions": [],
        },
        "bootstrap_samples": 10_000,
        "seed": 20260809,
        "candidates": [CaptureExpected(candidate_id="candidate.a")],
        "interactions": [],
    }
    candidate_module._validate_study_against_raw_runs(
        study=study,
        baseline=baseline,
        candidate_runs={"candidate.a": candidate},
        interaction_runs={},
        catalog=catalog,
        raw_verifications={
            baseline["run_id"]: study["raw_evidence"]["baseline"],
            candidate["run_id"]: study["raw_evidence"]["candidates"][0]["run"],
        },
    )
    study["candidates"] = [captured]
    raw_verifications = {
        baseline["run_id"]: study["raw_evidence"]["baseline"],
        candidate["run_id"]: study["raw_evidence"]["candidates"][0]["run"],
    }
    candidate_module._validate_study_against_raw_runs(
        study=study,
        baseline=baseline,
        candidate_runs={"candidate.a": candidate},
        interaction_runs={},
        catalog=catalog,
        raw_verifications=raw_verifications,
    )

    tampered = deepcopy(study)
    tampered["candidates"][0]["case_geometric_mean_speedup"] *= 1.01
    with pytest.raises(ValidationError, match="metrics differ from raw run"):
        candidate_module._validate_study_against_raw_runs(
            study=tampered,
            baseline=baseline,
            candidate_runs={"candidate.a": candidate},
            interaction_runs={},
            catalog=catalog,
            raw_verifications=raw_verifications,
        )


def test_candidate_final_identity_rejects_cross_campaign_reuse() -> None:
    raw_registry = {
        "path": "raw/snapshot.json",
        "canonical_sha256": "7" * 64,
        "physical_sha256": "8" * 64,
    }
    plan = {
        "campaign_id": "campaign-a",
        "run_namespace": "campaign-a:",
        "repository": {
            "repo_commit": "1" * 40,
            "repo_tree": "2" * 40,
            "compiler_artifact": {
                "path": "build/compiler.jar",
                "physical_sha256": "3" * 64,
            },
        },
    }
    previous = {
        "campaign_id": "campaign-a",
        "plan_sha256": sha256_json(plan),
        "ready_tasks": ["final"],
        "tasks": [{"task_id": "final", "status": "pending"}],
        "raw_evidence_registry": raw_registry,
    }
    freeze = {
        "freeze_id": "freeze-1",
        "campaign_id": "campaign-a",
        "b2_campaign": {"raw_evidence_registry": raw_registry},
    }
    final = {
        "campaign": {
            "plan_sha256": sha256_json(plan),
            "status_sha256": sha256_json(previous),
            "status_ledger_head_sha256": sha256_json(previous),
            "raw_evidence_registry": raw_registry,
        },
        "freeze": {
            "freeze_id": "freeze-1",
            "freeze_sha256": sha256_json(freeze),
            "campaign_id": "campaign-a",
            "run_namespace": "campaign-a:",
            "repo_commit": plan["repository"]["repo_commit"],
            "repo_tree": plan["repository"]["repo_tree"],
            "compiler_artifact": plan["repository"]["compiler_artifact"],
            "raw_evidence_registry": raw_registry,
        },
    }
    candidate_module._require_candidate_final_campaign_identity(
        final=final, plan=plan, previous=previous, freeze=freeze
    )

    cross_campaign = deepcopy(final)
    cross_campaign["freeze"]["campaign_id"] = "campaign-b"
    with pytest.raises(ValidationError, match="exact campaign/freeze/status"):
        candidate_module._require_candidate_final_campaign_identity(
            final=cross_campaign,
            plan=plan,
            previous=previous,
            freeze=freeze,
        )

    expected = {
        "campaign": final["campaign"],
        "freeze": final["freeze"],
        "candidates": [
            {
                "candidate_id": "candidate.a",
                "combined_case_geometric_mean_speedup": 1.02,
            }
        ],
        "ranking": [{"candidate_id": "candidate.a", "rank": 1}],
    }
    candidate_module._require_exact_candidate_final_derivation(
        expected, deepcopy(expected)
    )
    fake_ranking = deepcopy(expected)
    fake_ranking["candidates"][0]["combined_case_geometric_mean_speedup"] = 1.20
    with pytest.raises(ValidationError, match="exact raw-study derivation"):
        candidate_module._require_exact_candidate_final_derivation(
            fake_ranking, expected
        )


def test_candidate_ledger_prefix_binds_physical_bytes() -> None:
    identities = [
        {
            "canonical_sha256": "1" * 64,
            "physical_sha256": "2" * 64,
        },
        {
            "canonical_sha256": "3" * 64,
            "physical_sha256": "4" * 64,
        },
    ]
    binding = {
        "status_sha256": identities[-1]["canonical_sha256"],
        "status_ledger_entry_count": 2,
        "status_ledger_head_sha256": identities[-1]["canonical_sha256"],
        "status_ledger_sha256": sha256_json(identities),
    }
    assert candidate_module._require_candidate_ledger_prefix(
        binding=binding,
        ledger_identities=identities,
        label="candidate freeze",
    ) == identities

    whitespace_rewrite = deepcopy(identities)
    whitespace_rewrite[0]["physical_sha256"] = "5" * 64
    with pytest.raises(ValidationError, match="physical status-ledger prefix"):
        candidate_module._require_candidate_ledger_prefix(
            binding=binding,
            ledger_identities=whitespace_rewrite,
            label="candidate freeze",
        )


def test_candidate_final_completion_rebuilds_all_derived_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _input_files(
        tmp_path,
        "plan.json",
        "final.json",
        "pre-final-status.json",
        "completed-status.json",
        "raw-registry.json",
        "run.json",
        "screening.json",
        "catalog.json",
        "matrix.json",
        "freeze.json",
        "b2-study.json",
        "b3-study.json",
    )
    plan = {
        "campaign_id": "campaign-a",
        "run_namespace": "campaign-a:",
        "artifacts": {
            "screening": {"path": "screening.json"},
            "candidate_registry": {"path": "catalog.json"},
            "matrix": {"path": "matrix.json"},
        },
    }
    pre_final = {"status": "ready-for-final"}
    raw_artifact = {
        "path": "raw-registry.json",
        "canonical_sha256": "a" * 64,
        "physical_sha256": "b" * 64,
    }
    final = {
        "final_id": "final-a",
        "campaign": {
            "plan_sha256": sha256_json(plan),
            "status_sha256": sha256_json(pre_final),
            "status_ledger_entry_count": 1,
            "status_ledger_head_sha256": sha256_json(pre_final),
            "status_ledger_sha256": sha256_json(
                [
                    {
                        "canonical_sha256": sha256_json(pre_final),
                        "physical_sha256": sha256_file(
                            paths["pre-final-status.json"]
                        ),
                    }
                ]
            ),
            "raw_evidence_registry": raw_artifact,
        },
        "freeze": {
            "campaign_id": "campaign-a",
            "run_namespace": "campaign-a:",
            "artifact": {
                "path": "freeze.json",
                "canonical_sha256": "c" * 64,
                "physical_sha256": "d" * 64,
            },
        },
        "studies": {
            "B2": {"path": "b2-study.json"},
            "B3": {"path": "b3-study.json"},
            "B4": None,
            "B5": None,
            "B6": None,
        },
        "diagnostics": {"study": None},
        "ranking": [{"candidate_id": "candidate.a", "rank": 1}],
    }
    completed = {
        "campaign_id": "campaign-a",
        "plan_sha256": sha256_json(plan),
        "state": "completed",
        "ready_tasks": [],
        "previous_status_sha256": sha256_json(pre_final),
        "raw_evidence_registry": raw_artifact,
        "tasks": [
            {
                "task_id": "final",
                "status": "completed",
                "evidence_kind": "candidate-final.v1",
                "evidence_sha256": sha256_json(final),
                "evidence_physical_sha256": sha256_file(paths["final.json"]),
            }
        ],
    }
    raw_registry = {
        "runs": [
            {
                "task_id": "run.B1.full",
                "run_record": {"path": "run.json"},
            }
        ]
    }
    documents = {
        paths["plan.json"].resolve(): plan,
        paths["final.json"].resolve(): final,
        paths["pre-final-status.json"].resolve(): pre_final,
        paths["completed-status.json"].resolve(): completed,
    }

    def fake_load(path: Path, version: str, *, label: str) -> dict[str, Any]:
        del version, label
        return deepcopy(documents[path.resolve()])

    def fake_ledger(**kwargs: Any) -> tuple[Any, ...]:
        del kwargs
        identities = [
            {
                "canonical_sha256": sha256_json(pre_final),
                "physical_sha256": sha256_file(paths["pre-final-status.json"]),
            },
            {
                "canonical_sha256": sha256_json(completed),
                "physical_sha256": sha256_file(paths["completed-status.json"]),
            },
        ]
        return 2, sha256_json(completed), sha256_json(identities), [], identities

    expected = deepcopy(final)
    monkeypatch.setattr(candidate_module, "_load_version", fake_load)
    monkeypatch.setattr(
        candidate_module,
        "_load_and_reverify_candidate_raw_evidence_registry",
        lambda **kwargs: (deepcopy(raw_registry), deepcopy(raw_artifact)),
    )
    monkeypatch.setattr(
        candidate_module, "_validate_candidate_status_ledger", fake_ledger
    )
    monkeypatch.setattr(
        candidate_module,
        "build_candidate_final",
        lambda **kwargs: deepcopy(expected),
    )

    closure = candidate_module.validate_candidate_final_completion(
        campaign_plan_path=paths["plan.json"],
        candidate_final_path=paths["final.json"],
        completed_status_path=paths["completed-status.json"],
        status_ledger_paths=[
            paths["pre-final-status.json"],
            paths["completed-status.json"],
        ],
        workspace_root=tmp_path,
    )
    assert closure["candidate_final_sha256"] == sha256_json(final)

    forged = deepcopy(final)
    forged["ranking"][0]["rank"] = 2
    documents[paths["final.json"].resolve()] = forged
    completed["tasks"][0]["evidence_sha256"] = sha256_json(forged)
    with pytest.raises(ValidationError, match="exact replayed campaign"):
        candidate_module.validate_candidate_final_completion(
            campaign_plan_path=paths["plan.json"],
            candidate_final_path=paths["final.json"],
            completed_status_path=paths["completed-status.json"],
            status_ledger_paths=[
                paths["pre-final-status.json"],
                paths["completed-status.json"],
            ],
            workspace_root=tmp_path,
        )


def test_candidate_b1_rejects_external_compiler_spoof() -> None:
    run = make_run(
        "run-b1",
        {"case-a": ("family", 1.0)},
        profile_id="candidate-empty",
    )
    run["configuration"]["evidence_level"] = "qemu_correctness"
    run["configuration"]["compiler"]["kind"] = "external"
    run["configuration"]["tool_versions"].append(
        {
            "tool": "qemu-system-riscv64",
            "actual": "9.2",
            "official_expected": None,
            "comparison": "unknown",
        }
    )
    with pytest.raises(ValidationError, match="must use BenchmarkCompiler"):
        candidate_module._require_candidate_correctness_run(
            run, label="campaign B1 task run.B1.full"
        )

    run["configuration"]["compiler"]["kind"] = "benchmark-compiler"
    candidate_module._require_candidate_correctness_run(
        run, label="campaign B1 task run.B1.full"
    )

    run["configuration"]["enabled_candidate_ids"] = ["candidate.a"]
    run["configuration"]["remarks_file_sha256"] = None
    for case in run["cases"]:
        case["remarks_sha256"] = None
        case["remarks_event_count"] = None
        case["candidate_remark_summary"] = None
    with pytest.raises(ValidationError, match="decision-observable remarks"):
        candidate_module._require_candidate_correctness_run(
            run, label="campaign B1 task run.B1.candidate.a"
        )


@pytest.mark.parametrize("terminal", ["timeout", "wrong_output", "tool_error"])
def test_candidate_b1_rejects_real_archived_failures(terminal: str) -> None:
    run = make_run(
        "run-b1",
        {"case-a": ("family", 1.0)},
        profile_id="candidate-empty",
    )
    run["configuration"]["evidence_level"] = "qemu_correctness"
    run["configuration"]["tool_versions"].append(
        {
            "tool": "qemu-system-riscv64",
            "actual": "9.2",
            "official_expected": None,
            "comparison": "unknown",
        }
    )
    run["cases"][0]["attempts"] = [
        {
            "attempt_index": 0,
            "status": terminal,
            "failure_summary": terminal,
            "cancellation_reason": None,
        }
    ]
    with pytest.raises(ValidationError, match="historical failed attempt"):
        candidate_module._require_candidate_correctness_run(
            run, label="campaign B1 task run.B1.full"
        )


def test_candidate_b1_accepts_only_typed_pre_phase_interruption_history() -> None:
    run = make_run(
        "run-b1",
        {"case-a": ("family", 1.0)},
        profile_id="candidate-empty",
    )
    run["configuration"]["evidence_level"] = "qemu_correctness"
    run["configuration"]["tool_versions"].append(
        {
            "tool": "qemu-system-riscv64",
            "actual": "9.2",
            "official_expected": None,
            "comparison": "unknown",
        }
    )
    case = run["cases"][0]
    started_at = "2026-08-09T00:00:00Z"
    case["attempts"] = [
        {
            "attempt_index": 0,
            "started_at": started_at,
            "archived_at": "2026-08-09T00:00:01Z",
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
            "cancellation_reason": "execution_interrupted",
            "cache_hit": False,
            "artifact_sha256": None,
            "binary_sha256": None,
            "remarks_sha256": None,
            "remarks_event_count": None,
            "candidate_remark_summary": None,
            "analysis_sha256": None,
            "attempt_journal_sha256": "a" * 64,
            "attempt_journal_event_count": 1,
            "compile": None,
            "compile_samples": [],
            "compile_statistics": None,
            "link": None,
            "analyze": None,
            "measurements": [],
            "samples": [],
            "consistency_passed": False,
            "consistency_mismatched_metrics": [],
            "diagnostic": "typed pre-phase interruption",
        }
    ]
    candidate_module._require_candidate_correctness_run(
        run, label="campaign B1 task run.B1.full"
    )


def test_candidate_study_preserves_nonlexical_catalog_order() -> None:
    catalog = {
        "candidates": [
            {"candidate_id": "candidate.z"},
            {"candidate_id": "candidate.a"},
        ]
    }
    paths = {
        "candidate.a": Path("a.json"),
        "candidate.z": Path("z.json"),
    }

    assert candidate_module._ordered_candidate_paths(catalog, paths) == [
        ("candidate.z", Path("z.json")),
        ("candidate.a", Path("a.json")),
    ]


def _candidate_freeze_document() -> dict[str, Any]:
    candidate_ids = ["candidate.alpha", "candidate.beta"]

    def artifact(path: str, seed: int) -> dict[str, str]:
        return {
            "path": path,
            "canonical_sha256": f"{seed:064x}",
            "physical_sha256": f"{seed + 1:064x}",
        }

    def protocol(mode: str, seed: int) -> dict[str, Any]:
        return {
            "protocol_id": f"protocol-{mode}",
            "measurement_mode": mode,
            "protocol_sha256": f"{seed:064x}",
            "path": f"protocols/{mode}.json",
            "physical_sha256": f"{seed + 10:064x}",
            "runner_command_sha256": f"{seed + 1:064x}",
            "runner_adapter": "wsl",
            "profile_plugin_sha256": f"{seed + 2:064x}",
            "cache_plugin_sha256": f"{seed + 3:064x}",
            "hotblocks_plugin_sha256": f"{seed + 4:064x}",
            "cache_model_sha256": f"{seed + 5:064x}",
        }

    return {
        "schema_version": "candidate-freeze.v1",
        "freeze_id": "freeze-1",
        "frozen_at": "2026-08-11T00:00:00Z",
        "campaign_id": "campaign-1",
        "run_namespace": "campaign-1:",
        "repository": {
            "repo_commit": "a" * 40,
            "repo_tree": "b" * 40,
            "repo_dirty": False,
            "tracked_diff_sha256": None,
            "compiler_artifact": {
                "path": "build/compiler.jar",
                "physical_sha256": "c" * 64,
            },
        },
        "snapshots": {
            "candidate_registry": artifact("data/catalog.json", 1),
            "screening_base_pass_registry": artifact(
                "data/pass-registry.base.v2.json", 3
            ),
            "executable_pass_registry": artifact(
                "data/pass-registry.executable.v2.json", 4
            ),
            "matrix": artifact("data/matrix.json", 5),
            "screening": artifact("data/screening.json", 7),
            "oracle_capture": artifact("data/oracle.json", 9),
            "run_record_schema": {
                "path": "tools/benchmark/schemas/run-record.v1.json",
                "canonical_sha256": sha256_json(
                    candidate_module.read_json(
                        Path("tools/benchmark/schemas/run-record.v1.json")
                    )
                ),
                "physical_sha256": schema_sha256("run-record.v1"),
            },
            "candidate_study_schema": {
                "path": "tools/benchmark/schemas/candidate-study.v1.json",
                "canonical_sha256": sha256_json(
                    candidate_module.read_json(
                        Path("tools/benchmark/schemas/candidate-study.v1.json")
                    )
                ),
                "physical_sha256": schema_sha256("candidate-study.v1"),
            },
            "candidate_evidence_sha256": "d" * 64,
            "screening_spec_sha256": "e" * 64,
            "oracle_plan_sha256": "f" * 64,
        },
        "base_pipeline_profile": {
            "profile_id": "candidate-empty",
            "artifact": artifact("profiles/candidate-empty.json", 11),
        },
        "suites": [
            {
                "data_role": role,
                "suite_id": f"suite-{role.lower()}",
                "manifest": artifact(
                    f"manifests/{role.lower()}.json", index + 20
                ),
                "case_count": count,
            }
            for index, (role, count) in enumerate(
                (("B1", 140), ("B2", 20), ("B3", 60), ("B4", 59), ("B5", 60), ("B6", 88))
            )
        ],
        "measurement_protocols": {
            "standard_proxy": protocol("standard_proxy", 40),
            "cache_hotblock": protocol("cache_hotblock", 60),
        },
        "reference_toolchain": {
            "snapshot": artifact("data/toolchain.json", 80),
            "compile_driver_sha256": "5" * 64,
            "source_adapter_sha256": "6" * 64,
            "builtin_header_sha256": "7" * 64,
            "image_id": "sha256:" + "8" * 64,
            "common_tool_versions": {
                "qemu-system-riscv64": "9.2",
                "bare-metal-linker": "2.42",
                "python": "3.12",
                "glib": "2.80",
            },
            "accela_jdk_version": "21",
            "baselines": [
                {
                    "compiler_baseline": "gcc_13_3_o2",
                    "profile_id": "gcc-13.3-o2",
                    "profile_sha256": "9" * 64,
                    "tool": "riscv-gcc",
                    "version": "13.3.0",
                    "optimization": "-O2",
                    "compiler_executable": "sh",
                    "compiler_command_sha256": "a" * 64,
                    "compiler_argv_sha256": "b" * 64,
                },
                {
                    "compiler_baseline": "clang_18_o3",
                    "profile_id": "clang-18-o3",
                    "profile_sha256": "c" * 64,
                    "tool": "clang",
                    "version": "18.1.3",
                    "optimization": "-O3",
                    "compiler_executable": "sh",
                    "compiler_command_sha256": "d" * 64,
                    "compiler_argv_sha256": "e" * 64,
                },
            ],
        },
        "gates": {
            "oracle_structure_geometric_mean_minimum": 1.1,
            "b3_geometric_mean_strictly_above": 1.0,
            "combined_case_count": 267,
            "evidence_claim": "qemu_proxy_only",
        },
        "ranking_rule": {
            "primary": "combined_geometric_mean_desc",
            "secondary": "b3_geometric_mean_desc",
            "tertiary": "static_text_bytes_asc",
            "quaternary": "stable_candidate_id_asc",
        },
        "frozen_candidate_ids": candidate_ids,
        "frozen_candidate_ids_sha256": sha256_json(candidate_ids),
        "b2_campaign": {
            "plan_sha256": "f" * 64,
            "status_sha256": "1" * 64,
            "status_ledger_entry_count": 1,
            "status_ledger_head_sha256": "1" * 64,
            "status_ledger_sha256": "4" * 64,
            "raw_evidence_registry": {
                "path": "raw/snapshot.json",
                "canonical_sha256": "5" * 64,
                "physical_sha256": "6" * 64,
            },
            "study_id": "study-b2",
            "study_sha256": "2" * 64,
            "b1_full_run_id": "campaign-1:run.B1.full",
            "b1_full_run_sha256": "3" * 64,
            "b1_passed_candidate_ids": ["candidate.alpha"],
            "b1_failed_candidate_ids": ["candidate.beta"],
        },
    }


def test_candidate_freeze_enforces_counts_namespace_and_hashes() -> None:
    freeze = _candidate_freeze_document()
    assert validate_document(freeze) == freeze

    wrong_count = json.loads(json.dumps(freeze))
    wrong_count["suites"][3]["case_count"] = 60
    with pytest.raises(ValidationError, match="case counts differ"):
        validate_document(wrong_count)

    wrong_namespace = json.loads(json.dumps(freeze))
    wrong_namespace["run_namespace"] = "another:"
    with pytest.raises(ValidationError, match="namespace differs"):
        validate_document(wrong_namespace)

    wrong_candidates = json.loads(json.dumps(freeze))
    wrong_candidates["frozen_candidate_ids"].reverse()
    with pytest.raises(ValidationError, match="identity hash"):
        validate_document(wrong_candidates)

    same_registry_hash = deepcopy(freeze)
    same_registry_hash["snapshots"]["executable_pass_registry"][
        "canonical_sha256"
    ] = same_registry_hash["snapshots"]["screening_base_pass_registry"][
        "canonical_sha256"
    ]
    with pytest.raises(ValidationError, match="must be distinct"):
        validate_document(same_registry_hash)

    same_registry_path = deepcopy(freeze)
    same_registry_path["snapshots"]["executable_pass_registry"]["path"] = (
        same_registry_path["snapshots"]["screening_base_pass_registry"]["path"]
    )
    with pytest.raises(ValidationError, match="must be distinct"):
        validate_document(same_registry_path)


def test_formal_suite_contract_rejects_equal_count_substitution_and_byte_drift(
    tmp_path: Path,
) -> None:
    source = Path(
        "docs/optimization/data/manifests/b3-official-performance-2026.manifest.json"
    ).resolve(strict=True)
    manifest = load_and_validate(source)
    require_formal_suite_contract(
        role="B3", manifest=manifest, manifest_path=source
    )

    substituted = json.loads(json.dumps(manifest))
    substituted["suite_id"] = "substituted-performance-suite"
    substituted_path = tmp_path / "substituted.json"
    substituted_path.write_text(
        json.dumps(substituted, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="locked canonical/physical"):
        require_formal_suite_contract(
            role="B3", manifest=substituted, manifest_path=substituted_path
        )

    byte_drift_path = tmp_path / "byte-drift.json"
    byte_drift_path.write_bytes(source.read_bytes() + b"\n")
    byte_drift = load_and_validate(byte_drift_path)
    assert sha256_json(byte_drift) == sha256_json(manifest)
    with pytest.raises(ValidationError, match="locked canonical/physical"):
        require_formal_suite_contract(
            role="B3", manifest=byte_drift, manifest_path=byte_drift_path
        )


def test_candidate_freeze_rejects_unbound_screening_oracle_capture() -> None:
    pass_registry = {"schema_version": "pass-registry.v2", "passes": []}
    capture = {
        "candidate_evidence_sha256": "a" * 64,
        "oracle_plan_sha256": "b" * 64,
    }
    screening = {
        "oracle_capture_sha256": sha256_json(capture),
        "candidate_evidence_sha256": capture["candidate_evidence_sha256"],
        "oracle_threshold": 1.10,
        "pass_registry_sha256": sha256_json(pass_registry),
        "base_pass_registry": {
            "path": "data/pass-registry.base.v2.json",
            "canonical_sha256": sha256_json(pass_registry),
            "physical_sha256": "d" * 64,
        },
    }
    candidate_module._require_screening_capture_binding(screening, capture)

    unrelated = json.loads(json.dumps(capture))
    unrelated["oracle_plan_sha256"] = "c" * 64
    with pytest.raises(ValidationError, match="binding differs"):
        candidate_module._require_screening_capture_binding(screening, unrelated)

    wrong_threshold = json.loads(json.dumps(screening))
    wrong_threshold["oracle_threshold"] = 1.09
    with pytest.raises(ValidationError, match="binding differs"):
        candidate_module._require_screening_capture_binding(wrong_threshold, capture)
