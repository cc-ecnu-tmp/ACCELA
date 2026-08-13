from __future__ import annotations

import shutil
import subprocess
from itertools import combinations
from pathlib import Path

import pytest

from tools.benchmark.errors import ValidationError
from tools.benchmark.fast_campaign import (
    FAST_ORACLE_STATIC_ARTIFACT_PATHS,
    FastRunAuthorizationIntent,
    authorize_fast_candidate_run_prelease,
    build_fast_audit,
    build_fast_campaign_plan,
    build_fast_campaign_status,
    build_fast_diagnostic_study,
    build_fast_final,
    build_fast_run_index,
    build_fast_study,
    fast_configuration_template_sha256,
    publish_fast_run_receipt,
    publish_immutable_fast_document,
)
from tools.benchmark.fast_report import build_fast_report
import tools.benchmark.fast_campaign as fast_campaign_module
from tools.benchmark.inventory import inventory_suite
from tools.benchmark.metrics import cache_hotblock_metrics_v1
from tools.benchmark.schema import validate_document
from tools.benchmark.tests.test_stats_ablation_report import (
    _rebind_synthetic_run_configuration,
    make_run,
)
from tools.benchmark.util import (
    atomic_write_json,
    read_json,
    sha256_artifact,
    sha256_file,
    sha256_json,
)


NOW = "2026-08-13T00:00:00Z"
REPOSITORY = {"commit": "1" * 40, "tree": "2" * 40, "dirty": False}
ORACLE_STATIC_IDS = (
    "candidate-screening",
    "candidate-oracle-capture",
    "candidate-evidence",
    "candidate-screening-spec",
)
REAL_QUALIFIED_CANDIDATE_IDS = (
    "candidate.extended-affine-summarization",
    "candidate.finite-state-acceleration",
    "candidate.same-domain-loop-fusion",
    "candidate.integer-linear-transition",
    "candidate.rrt2-on-demand-memoization",
    "candidate.prefix-scan-reuse",
)


def artifact(root: Path, path: Path) -> dict[str, str]:
    physical = path.resolve(strict=True)
    relative = physical.relative_to(root.resolve(strict=True)).as_posix()
    if physical.suffix == ".json":
        canonical = sha256_json(__import__("json").loads(physical.read_text(encoding="utf-8")))
    else:
        canonical = sha256_file(physical)
    return {
        "path": relative,
        "canonical_sha256": canonical,
        "physical_sha256": sha256_file(physical),
    }


def committed(document: dict, field: str) -> dict:
    document[field] = sha256_json(
        {key: value for key, value in document.items() if key != field}
    )
    return validate_document(document)


def write_bootstrap(root: Path, *, real_oracle: bool = False) -> Path:
    source = root / "source.json"
    static = root / "static.json"
    atomic_write_json(source, {"source": True})
    atomic_write_json(static, {"static": True})
    source_row = {"artifact_id": "source", "artifact": artifact(root, source)}
    source_row["verification_commitment_sha256"] = sha256_json(source_row)
    static_row = {"artifact_id": "static", "artifact": artifact(root, static)}
    static_row["verification_commitment_sha256"] = sha256_json(static_row)
    oracle_rows = []
    repository_root = Path(__file__).resolve().parents[3]
    for artifact_id in ORACLE_STATIC_IDS:
        if real_oracle:
            oracle_path = root / FAST_ORACLE_STATIC_ARTIFACT_PATHS[artifact_id]
            oracle_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                repository_root / FAST_ORACLE_STATIC_ARTIFACT_PATHS[artifact_id],
                oracle_path,
            )
        else:
            oracle_path = root / f"{artifact_id}.json"
            atomic_write_json(oracle_path, {"artifact_id": artifact_id})
        row = {"artifact_id": artifact_id, "artifact": artifact(root, oracle_path)}
        row["verification_commitment_sha256"] = sha256_json(row)
        oracle_rows.append(row)
    if real_oracle:
        capture = read_json(root / FAST_ORACLE_STATIC_ARTIFACT_PATHS["candidate-oracle-capture"])
        oracle_plan = capture["sources"]["oracle_plan"]["path"]
        destination = root / oracle_plan
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repository_root / oracle_plan, destination)
    document = committed(
        {
            "schema_version": "candidate-fast-bootstrap.v1",
            "bootstrap_id": "bootstrap",
            "campaign_id": "fast-campaign",
            "created_at": NOW,
            "source_revision": REPOSITORY,
            "evaluation_revision": REPOSITORY,
            "source_artifacts": [source_row],
            "static_artifacts": [static_row, *oracle_rows],
            "imported_receipts": [],
            "bootstrap_commitment_sha256": "0" * 64,
        },
        "bootstrap_commitment_sha256",
    )
    path = root / "bootstrap.json"
    publish_immutable_fast_document(path, document)
    return path


def bootstrap_static_bindings(root: Path) -> list[dict]:
    bootstrap = read_json(root / "bootstrap.json")
    return [
        {"artifact_id": row["artifact_id"], "artifact": row["artifact"]}
        for row in bootstrap["static_artifacts"]
    ]


def prepare_campaign(root: Path) -> tuple[FastRunAuthorizationIntent, dict, dict]:
    bootstrap_path = write_bootstrap(root)
    manifest_path = root / "manifest.json"
    profile_path = root / "profile.json"
    protocol_path = root / "protocol.json"
    compiler_path = root / "compiler.bin"
    suite = root / "suite"
    suite.mkdir()
    (suite / "case.sy").write_text("int main(){return 0;}\n", encoding="utf-8")
    (suite / "case.in").write_bytes(b"")
    (suite / "case.out").write_bytes(b"0\n")
    manifest = inventory_suite(
        suite,
        suite_id="suite-B2",
        target="rv64gc",
        data_role="B3",
        origin_source="fast-test",
        origin_snapshot_sha256="a" * 64,
        license_expression="NOASSERTION",
        captured_at=NOW,
    )
    manifest["provenance"]["data_role"] = "B2"
    manifest["provenance"]["derived_from"] = {
        "suite_id": "suite-parent",
        "manifest_sha256": "a" * 64,
        "origin_source_id": "fast-test-parent",
        "origin_snapshot_sha256": "a" * 64,
    }
    for case in manifest["cases"]:
        case["provenance"]["data_role"] = "B2"
        case["provenance"]["derived_from"] = dict(
            manifest["provenance"]["derived_from"]
        )
    manifest = validate_document(manifest)
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(profile_path, {"profile_id": "candidate-empty"})
    atomic_write_json(protocol_path, {"protocol_id": "standard"})
    compiler_path.write_bytes(b"compiler")

    run = make_run(
        "fast-campaign:run.B2.full",
        {"case": ("family", 100.0)},
        profile_id="candidate-empty",
        profile_sha256=sha256_file(profile_path),
    )
    run["suite_id"] = "suite-B2"
    run["manifest_sha256"] = sha256_json(manifest)
    run["provenance"].update(
        {
            "repo_commit": REPOSITORY["commit"],
            "repo_dirty": False,
            "tracked_diff_sha256": None,
            "compiler_artifact_sha256": sha256_artifact(compiler_path),
            "execution_environment_sha256": "3" * 64,
            "measurement_protocol_sha256": sha256_json({"protocol_id": "standard"}),
        }
    )
    run["configuration"].update(
        {
            "enabled_candidate_ids": [],
            "repetitions": 1,
            "max_workers": 4,
            "keep_going": False,
            "retry_failures": False,
        }
    )
    _rebind_synthetic_run_configuration(run)
    validate_document(run)
    run_path = root / "run.json"
    atomic_write_json(run_path, run)

    task = {
        "ordinal": 0,
        "task_id": "run.B2.full",
        "kind": "run",
        "run_kind": "candidate_empty",
        "stage": "B2",
        "candidate_ids": [],
        "data_role": "B2",
        "measurement_mode": "standard_proxy",
        "dependencies": [],
        "terminal_dependencies": [],
        "gate": "always",
        "static_bindings": bootstrap_static_bindings(root),
        "output_path": "run.json",
        "receipt_path": "receipt.json",
        "run_id": run["run_id"],
        "logical_profile_id": "candidate-empty",
        "reference_profile_id": None,
        "reference_profile_sha256": None,
        "expected_configuration_template_sha256": fast_configuration_template_sha256(
            run["configuration"], run["provenance"], None
        ),
        "baseline_task_id": None,
        "baseline_artifact": None,
        "ranking_evidence": False,
        "suite_id": "suite-B2",
        "expected_case_count": 1,
        "manifest": artifact(root, manifest_path),
        "profile": artifact(root, profile_path),
        "measurement_protocol": artifact(root, protocol_path),
        "compiler_artifact": artifact(root, compiler_path),
        "execution_environment_sha256": "3" * 64,
    }
    plan = build_fast_campaign_plan(
        plan_id="plan",
        bootstrap_path=bootstrap_path,
        workspace_root=root,
        tasks=[task],
        candidate_ids=["candidate-a"],
        created_at=NOW,
    )
    plan_path = root / "plan.json"
    publish_immutable_fast_document(plan_path, plan)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=root,
        generated_at=NOW,
    )
    index_path = root / "index.json"
    publish_immutable_fast_document(index_path, index)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=root,
        generation=0,
        generated_at=NOW,
    )
    status_path = root / "status.json"
    publish_immutable_fast_document(status_path, status)
    intent = FastRunAuthorizationIntent(
        plan_path=plan_path,
        status_path=status_path,
        index_path=index_path,
        task_id=task["task_id"],
        workspace_root=root,
        manifest_path=manifest_path,
        output_path=run_path,
        compiler_artifact_path=compiler_path,
        pipeline_profile_path=profile_path,
        measurement_protocol_path=protocol_path,
        baseline_run_path=None,
        configuration=run["configuration"],
        provenance=run["provenance"],
        run_id=run["run_id"],
        receipt_path=root / "receipt.json",
    )
    return intent, plan, run


def _integration_manifest(root: Path, stage: str) -> Path:
    suite = root / f"suite-{stage}"
    suite.mkdir()
    (suite / "case.sy").write_text("int main(){return 0;}\n", encoding="utf-8")
    (suite / "case.in").write_bytes(b"")
    (suite / "case.out").write_bytes(b"0\n")
    manifest = inventory_suite(
        suite,
        suite_id=f"suite-{stage}",
        target="rv64gc",
        data_role="B3",
        origin_source="fast-integration-test",
        origin_snapshot_sha256="a" * 64,
        license_expression="NOASSERTION",
        captured_at=NOW,
    )
    if stage != "B3":
        derived_from = {
            "suite_id": "suite-B3",
            "manifest_sha256": "a" * 64,
            "origin_source_id": "fast-integration-parent",
            "origin_snapshot_sha256": "a" * 64,
        }
        manifest["provenance"]["data_role"] = stage
        manifest["provenance"]["derived_from"] = derived_from
        for case in manifest["cases"]:
            case["provenance"]["data_role"] = stage
            case["provenance"]["derived_from"] = dict(derived_from)
    manifest = validate_document(manifest)
    path = root / f"manifest-{stage}.json"
    atomic_write_json(path, manifest)
    return path


def _integration_run(
    *,
    task: dict,
    root: Path,
    metric_value: float,
    compiler_path: Path,
    manifest_path: Path,
    profile_path: Path,
    protocol_path: Path,
    baseline_run_path: Path | None,
) -> dict:
    run = make_run(
        task["run_id"],
        {"case": ("family", metric_value)},
        profile_id=task["logical_profile_id"],
        profile_sha256=sha256_file(profile_path),
    )
    run["suite_id"] = task["suite_id"]
    run["manifest_sha256"] = sha256_json(read_json(manifest_path))
    run["provenance"].update(
        {
            "repo_commit": REPOSITORY["commit"],
            "repo_dirty": False,
            "tracked_diff_sha256": None,
            "compiler_artifact_sha256": sha256_artifact(compiler_path),
            "execution_environment_sha256": "3" * 64,
            "pipeline_profile_id": task["logical_profile_id"],
            "pipeline_profile_sha256": sha256_file(profile_path),
            "measurement_protocol_sha256": sha256_json(read_json(protocol_path)),
        }
    )
    run["configuration"].update(
        {
            "enabled_candidate_ids": list(task["candidate_ids"]),
            "compile_repetitions": 5,
            "reuse_compile_cache": False,
            "repetitions": 1,
            "max_workers": 4,
            "keep_going": False,
            "retry_failures": False,
            "seed": 20260809,
            "consistency_fraction": 0.1,
            "consistency_repetitions": 3,
        }
    )
    if task["candidate_ids"]:
        run["configuration"]["candidate_registry_sha256"] = "a" * 64
        run["configuration"]["candidate_pass_registry_sha256"] = "b" * 64
        for case in run["cases"]:
            case["candidate_remark_summary"] = {
                "event_count": case["remarks_event_count"],
                "summary_count": 1,
                "paired_candidate_count": 0,
                "applied_count": 0,
                "rejected_count": 0,
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "paired_candidate_count": 0,
                        "applied_count": 0,
                        "rejected_count": 0,
                    }
                    for candidate_id in task["candidate_ids"]
                ],
            }
    if task["measurement_mode"] == "cache_hotblock":
        extension = cache_hotblock_metrics_v1()
        run["configuration"]["metrics"].extend(
            {
                "metric_id": item["metric_id"],
                "source": item["source"],
                "pattern_sha256": sha256_json(item["pattern"]),
                "unit": item["unit"],
            }
            for item in extension
        )
        values = {
            "hotblock_hottest_address": 0x800003BC,
            "hotblock_hottest_executions": 20,
            "hotblock_hottest_dynamic_instructions": 80,
        }
        for case in run["cases"]:
            for sample in case["samples"]:
                sample["measurements"].extend(
                    {
                        "metric_id": item["metric_id"],
                        "value": float(values[item["metric_id"]]),
                        "unit": item["unit"],
                        "origin": "observed",
                        "availability": "measured",
                        "reason": None,
                    }
                    for item in extension
                )
    if baseline_run_path is None:
        run["configuration"]["timeout_policy"] = "initial"
        run["configuration"]["baseline_timeout_run_id"] = None
        run["configuration"]["baseline_timeout_run_sha256"] = None
    else:
        baseline = read_json(baseline_run_path)
        run["configuration"]["timeout_policy"] = "baseline_derived"
        run["configuration"]["baseline_timeout_run_id"] = baseline["run_id"]
        run["configuration"]["baseline_timeout_run_sha256"] = sha256_json(baseline)
        for case in run["cases"]:
            case["effective_timeout_seconds"] = 120.0
            case["timeout_derivation"] = {
                "baseline_run_id": baseline["run_id"],
                "baseline_run_sha256": sha256_json(baseline),
                "baseline_case_status": "passed",
                "baseline_median_duration_ns": 10.0,
            }
    _rebind_synthetic_run_configuration(run)
    return validate_document(run)


def _integration_pseudo_task(
    *,
    ordinal: int,
    task_id: str,
    kind: str,
    stage: str,
    static_bindings: list[dict],
    candidate_ids: list[str] | None = None,
    dependencies: list[str] | None = None,
    terminal_dependencies: list[str] | None = None,
    gate: str = "always",
) -> dict:
    return {
        "ordinal": ordinal,
        "task_id": task_id,
        "kind": kind,
        "run_kind": None,
        "stage": stage,
        "candidate_ids": list(candidate_ids or []),
        "data_role": stage,
        "measurement_mode": "none",
        "dependencies": list(dependencies or []),
        "terminal_dependencies": list(terminal_dependencies or []),
        "gate": gate,
        "static_bindings": static_bindings,
        "output_path": f"{task_id}.json",
        "receipt_path": None,
        "run_id": None,
        "logical_profile_id": None,
        "reference_profile_id": None,
        "reference_profile_sha256": None,
        "expected_configuration_template_sha256": None,
        "baseline_task_id": None,
        "baseline_artifact": None,
        "ranking_evidence": False,
        "suite_id": None,
        "expected_case_count": None,
        "manifest": None,
        "profile": None,
        "measurement_protocol": None,
        "compiler_artifact": None,
        "execution_environment_sha256": None,
    }


def _prepare_real_diagnostic_campaign(
    root: Path,
    *,
    candidate_metrics: dict[str, float],
    include_b2_and_audits: bool,
) -> dict:
    root.mkdir()
    (root / "runs").mkdir()
    (root / "receipts").mkdir()
    bootstrap_path = write_bootstrap(root, real_oracle=include_b2_and_audits)
    compiler_path = root / "compiler.bin"
    compiler_path.write_bytes(b"compiler")
    standard_protocol_path = root / "protocol-standard.json"
    cache_protocol_path = root / "protocol-cache.json"
    atomic_write_json(standard_protocol_path, {"protocol_id": "standard"})
    atomic_write_json(cache_protocol_path, {"protocol_id": "cache-hotblock"})
    manifest_paths = {"B3": _integration_manifest(root, "B3")}
    if include_b2_and_audits:
        manifest_paths["B2"] = _integration_manifest(root, "B2")
    static_bindings = bootstrap_static_bindings(root)
    candidate_ids = list(candidate_metrics)
    profile_paths: dict[str, Path] = {}
    tasks: list[dict] = []

    def measured_task(
        *,
        task_id: str,
        kind: str,
        stage: str,
        candidate_ids_for_task: list[str],
        measurement_mode: str,
        dependencies: list[str],
        gate: str,
        baseline_task_id: str | None,
        run_kind: str | None,
        ranking_evidence: bool = False,
    ) -> dict:
        logical_profile_id = task_id
        profile_path = root / f"profile-{task_id}.json"
        atomic_write_json(
            profile_path,
            {
                "profile_id": logical_profile_id,
                "enable_candidates": candidate_ids_for_task,
            },
        )
        profile_paths[task_id] = profile_path
        protocol_path = (
            cache_protocol_path
            if measurement_mode == "cache_hotblock"
            else standard_protocol_path
        )
        manifest_path = manifest_paths["B3" if stage == "diagnostic" else stage]
        task = {
            "ordinal": len(tasks),
            "task_id": task_id,
            "kind": kind,
            "run_kind": run_kind,
            "stage": stage,
            "candidate_ids": candidate_ids_for_task,
            "data_role": "B3" if stage == "diagnostic" else stage,
            "measurement_mode": measurement_mode,
            "dependencies": dependencies,
            "terminal_dependencies": [],
            "gate": gate,
            "static_bindings": static_bindings,
            "output_path": f"runs/{task_id}.json",
            "receipt_path": f"receipts/{task_id}.json",
            "run_id": f"fast-campaign:{task_id}",
            "logical_profile_id": logical_profile_id,
            "reference_profile_id": None,
            "reference_profile_sha256": None,
            "expected_configuration_template_sha256": "0" * 64,
            "baseline_task_id": baseline_task_id,
            "baseline_artifact": None,
            "ranking_evidence": ranking_evidence,
            "suite_id": f"suite-{'B3' if stage == 'diagnostic' else stage}",
            "expected_case_count": 1,
            "manifest": artifact(root, manifest_path),
            "profile": artifact(root, profile_path),
            "measurement_protocol": artifact(root, protocol_path),
            "compiler_artifact": artifact(root, compiler_path),
            "execution_environment_sha256": "3" * 64,
        }
        prototype = _integration_run(
            task=task,
            root=root,
            metric_value=100.0,
            compiler_path=compiler_path,
            manifest_path=manifest_path,
            profile_path=profile_path,
            protocol_path=protocol_path,
            baseline_run_path=None,
        )
        if baseline_task_id is not None:
            prototype["configuration"]["timeout_policy"] = "baseline_derived"
            _rebind_synthetic_run_configuration(prototype)
        task["expected_configuration_template_sha256"] = (
            fast_configuration_template_sha256(
                prototype["configuration"],
                prototype["provenance"],
                baseline_task_id,
            )
        )
        tasks.append(task)
        return task

    audit_paths: dict[str, Path] = {}
    study_paths: dict[str, Path] = {}
    if include_b2_and_audits:
        tasks.append(
            _integration_pseudo_task(
                ordinal=len(tasks),
                task_id="audit.bootstrap",
                kind="audit",
                stage="bootstrap",
                static_bindings=static_bindings,
            )
        )
        measured_task(
            task_id="run.B2.full",
            kind="run",
            stage="B2",
            candidate_ids_for_task=[],
            measurement_mode="standard_proxy",
            dependencies=[],
            gate="always",
            baseline_task_id=None,
            run_kind="candidate_empty",
        )
        b2_candidate_task_ids: list[str] = []
        for candidate_id in candidate_ids:
            task_id = f"run.B2.{candidate_id}"
            measured_task(
                task_id=task_id,
                kind="run",
                stage="B2",
                candidate_ids_for_task=[candidate_id],
                measurement_mode="standard_proxy",
                dependencies=["run.B2.full"],
                gate="dependencies_succeeded",
                baseline_task_id="run.B2.full",
                run_kind="single",
            )
            b2_candidate_task_ids.append(task_id)
        tasks.append(
            _integration_pseudo_task(
                ordinal=len(tasks),
                task_id="study.B2",
                kind="study",
                stage="B2",
                static_bindings=static_bindings,
                candidate_ids=candidate_ids,
                dependencies=["run.B2.full"],
                terminal_dependencies=b2_candidate_task_ids,
                gate="dependencies_terminal",
            )
        )
        tasks.append(
            _integration_pseudo_task(
                ordinal=len(tasks),
                task_id="audit.B2",
                kind="audit",
                stage="B2",
                static_bindings=static_bindings,
                dependencies=["study.B2"],
                gate="dependencies_succeeded",
            )
        )
        b3_full_dependencies = ["audit.B2"]
    else:
        b3_full_dependencies = []
    measured_task(
        task_id="run.B3.full",
        kind="run",
        stage="B3",
        candidate_ids_for_task=[],
        measurement_mode="standard_proxy",
        dependencies=b3_full_dependencies,
        gate="dependencies_succeeded" if b3_full_dependencies else "always",
        baseline_task_id=None,
        run_kind="candidate_empty",
    )
    b3_candidate_task_ids: list[str] = []
    for candidate_id in candidate_ids:
        task_id = f"run.B3.{candidate_id}"
        measured_task(
            task_id=task_id,
            kind="run",
            stage="B3",
            candidate_ids_for_task=[candidate_id],
            measurement_mode="standard_proxy",
            dependencies=["run.B3.full"],
            gate="dependencies_succeeded",
            baseline_task_id="run.B3.full",
            run_kind="single",
            ranking_evidence=True,
        )
        b3_candidate_task_ids.append(task_id)
    tasks.append(
        _integration_pseudo_task(
            ordinal=len(tasks),
            task_id="study.B3",
            kind="study",
            stage="B3",
            static_bindings=static_bindings,
            candidate_ids=candidate_ids,
            dependencies=["run.B3.full"],
            terminal_dependencies=b3_candidate_task_ids,
            gate="dependencies_terminal",
        )
    )
    if include_b2_and_audits:
        tasks.append(
            _integration_pseudo_task(
                ordinal=len(tasks),
                task_id="audit.B3",
                kind="audit",
                stage="B3",
                static_bindings=static_bindings,
                dependencies=["study.B3"],
                gate="dependencies_succeeded",
            )
        )
    diagnostic_task_ids: list[str] = []
    for pair in combinations(candidate_ids, 2):
        selected = sorted(pair)
        task_id = f"diagnostic.pair.{'+'.join(selected)}"
        measured_task(
            task_id=task_id,
            kind="diagnostic",
            stage="diagnostic",
            candidate_ids_for_task=selected,
            measurement_mode="standard_proxy",
            dependencies=["run.B3.full", "study.B3"],
            gate="diagnostic_top3",
            baseline_task_id="run.B3.full",
            run_kind=None,
        )
        diagnostic_task_ids.append(task_id)
    measured_task(
        task_id="diagnostic.cache.full",
        kind="diagnostic",
        stage="diagnostic",
        candidate_ids_for_task=[],
        measurement_mode="cache_hotblock",
        dependencies=["study.B3"],
        gate="dependencies_succeeded",
        baseline_task_id=None,
        run_kind=None,
    )
    diagnostic_task_ids.append("diagnostic.cache.full")
    for candidate_id in candidate_ids:
        task_id = f"diagnostic.cache.{candidate_id}"
        measured_task(
            task_id=task_id,
            kind="diagnostic",
            stage="diagnostic",
            candidate_ids_for_task=[candidate_id],
            measurement_mode="cache_hotblock",
            dependencies=["study.B3", "diagnostic.cache.full"],
            gate="diagnostic_top3",
            baseline_task_id="diagnostic.cache.full",
            run_kind=None,
        )
        diagnostic_task_ids.append(task_id)
    tasks.append(
        _integration_pseudo_task(
            ordinal=len(tasks),
            task_id="study.diagnostic",
            kind="study",
            stage="diagnostic",
            static_bindings=static_bindings,
            terminal_dependencies=diagnostic_task_ids,
            gate="dependencies_terminal",
        )
    )
    if include_b2_and_audits:
        tasks.append(
            _integration_pseudo_task(
                ordinal=len(tasks),
                task_id="audit.final",
                kind="audit",
                stage="final",
                static_bindings=static_bindings,
                dependencies=["study.diagnostic"],
                gate="dependencies_succeeded",
            )
        )
    tasks.append(
        _integration_pseudo_task(
            ordinal=len(tasks),
            task_id="final",
            kind="final",
            stage="final",
            static_bindings=static_bindings,
            dependencies=(
                ["audit.final", "study.diagnostic"]
                if include_b2_and_audits
                else ["study.diagnostic"]
            ),
            gate="final_ready",
        )
    )
    plan = build_fast_campaign_plan(
        plan_id="integration-plan",
        bootstrap_path=bootstrap_path,
        workspace_root=root,
        tasks=tasks,
        candidate_ids=candidate_ids,
        created_at=NOW,
    )
    plan_path = root / "plan.json"
    publish_immutable_fast_document(plan_path, plan)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=root,
        generated_at=NOW,
    )
    index_path = root / "index-0.json"
    publish_immutable_fast_document(index_path, index)
    return {
        "root": root,
        "bootstrap_path": bootstrap_path,
        "compiler_path": compiler_path,
        "standard_protocol_path": standard_protocol_path,
        "cache_protocol_path": cache_protocol_path,
        "manifest_paths": manifest_paths,
        "profile_paths": profile_paths,
        "plan": plan,
        "plan_path": plan_path,
        "index_path": index_path,
        "receipt_paths": {},
        "run_paths": {},
        "audit_paths": audit_paths,
        "study_paths": study_paths,
        "generation": 0,
        "candidate_ids": candidate_ids,
    }


def _integration_status(
    context: dict,
    *,
    name: str,
    diagnostic_study_path: Path | None = None,
    final_path: Path | None = None,
) -> tuple[dict, Path]:
    context["generation"] += 1
    status = build_fast_campaign_status(
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        workspace_root=context["root"],
        generation=context["generation"],
        study_paths=list(context["study_paths"].values()),
        audit_paths=list(context["audit_paths"].values()),
        diagnostic_paths=[
            path
            for task_id, path in context["receipt_paths"].items()
            if task_id.startswith("diagnostic.")
        ],
        diagnostic_study_path=diagnostic_study_path,
        final_path=final_path,
        generated_at=NOW,
    )
    path = context["root"] / f"status-{name}.json"
    publish_immutable_fast_document(path, status)
    return status, path


def _integration_publish_run(
    context: dict,
    *,
    task_id: str,
    metric_value: float,
    status_path: Path,
) -> Path:
    task = next(
        task for task in context["plan"]["tasks"] if task["task_id"] == task_id
    )
    manifest_path = context["manifest_paths"][
        "B3" if task["stage"] == "diagnostic" else task["stage"]
    ]
    protocol_path = (
        context["cache_protocol_path"]
        if task["measurement_mode"] == "cache_hotblock"
        else context["standard_protocol_path"]
    )
    baseline_path = (
        None
        if task["baseline_task_id"] is None
        else context["run_paths"][task["baseline_task_id"]]
    )
    run = _integration_run(
        task=task,
        root=context["root"],
        metric_value=metric_value,
        compiler_path=context["compiler_path"],
        manifest_path=manifest_path,
        profile_path=context["profile_paths"][task_id],
        protocol_path=protocol_path,
        baseline_run_path=baseline_path,
    )
    run_path = context["root"] / task["output_path"]
    atomic_write_json(run_path, run)
    receipt_path = context["root"] / task["receipt_path"]
    intent = FastRunAuthorizationIntent(
        plan_path=context["plan_path"],
        status_path=status_path,
        index_path=context["index_path"],
        task_id=task_id,
        workspace_root=context["root"],
        manifest_path=manifest_path,
        output_path=run_path,
        compiler_artifact_path=context["compiler_path"],
        pipeline_profile_path=context["profile_paths"][task_id],
        measurement_protocol_path=protocol_path,
        baseline_run_path=baseline_path,
        configuration=run["configuration"],
        provenance=run["provenance"],
        run_id=run["run_id"],
        receipt_path=receipt_path,
    )
    publish_fast_run_receipt(
        intent=intent,
        run_record_path=run_path,
        receipt_output_path=receipt_path,
    )
    context["run_paths"][task_id] = run_path
    context["receipt_paths"][task_id] = receipt_path
    return receipt_path


def _integration_advance_index(context: dict, name: str) -> Path:
    index = build_fast_run_index(
        plan_path=context["plan_path"],
        receipt_paths=list(context["receipt_paths"].values()),
        workspace_root=context["root"],
        previous_index_path=context["index_path"],
        generated_at=NOW,
    )
    path = context["root"] / f"index-{name}.json"
    publish_immutable_fast_document(path, index)
    context["index_path"] = path
    return path


def _integration_publish_stage_study(context: dict, stage: str) -> Path:
    candidate_paths = {
        candidate_id: context["receipt_paths"][f"run.{stage}.{candidate_id}"]
        for candidate_id in context["candidate_ids"]
    }
    study = build_fast_study(
        stage=stage,
        bootstrap_path=context["bootstrap_path"],
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        baseline_receipt_path=context["receipt_paths"][f"run.{stage}.full"],
        candidate_receipt_paths=candidate_paths,
        workspace_root=context["root"],
        generated_at=NOW,
    )
    path = context["root"] / f"study-{stage}.json"
    publish_immutable_fast_document(path, study)
    context["study_paths"][stage] = path
    return path


def _integration_publish_audit(
    context: dict, checkpoint: str, status_path: Path
) -> Path:
    audit = build_fast_audit(
        checkpoint=checkpoint,
        bootstrap_path=context["bootstrap_path"],
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        status_path=status_path,
        workspace_root=context["root"],
        generated_at=NOW,
    )
    path = context["root"] / f"audit-{checkpoint}.json"
    publish_immutable_fast_document(path, audit)
    context["audit_paths"][checkpoint] = path
    return path


def test_fast_prelease_and_receipt_bind_exact_normalized_run(tmp_path: Path) -> None:
    intent, _, run = prepare_campaign(tmp_path)
    assert authorize_fast_candidate_run_prelease(intent)["run_id"] == run["run_id"]
    receipt = publish_fast_run_receipt(
        intent=intent,
        run_record_path=tmp_path / "run.json",
        receipt_output_path=tmp_path / "receipt.json",
        state_root=tmp_path / "must-not-be-read",
    )
    assert receipt["run_id"] == run["run_id"]
    assert receipt["correctness"]["all_correct"] is True
    assert receipt["metrics"]["sample_count"] == 1
    assert publish_fast_run_receipt(
        intent=intent,
        run_record_path=tmp_path / "run.json",
        receipt_output_path=tmp_path / "receipt.json",
    ) == receipt


def test_fast_receipt_rejects_unplanned_publication_path(tmp_path: Path) -> None:
    intent, _, _ = prepare_campaign(tmp_path)
    with pytest.raises(ValidationError, match="planned receipt path"):
        publish_fast_run_receipt(
            intent=intent,
            run_record_path=tmp_path / "run.json",
            receipt_output_path=tmp_path / "other-receipt.json",
        )


def test_fast_prelease_rejects_configuration_template_drift(tmp_path: Path) -> None:
    intent, _, _ = prepare_campaign(tmp_path)
    drifted = FastRunAuthorizationIntent(
        **{**intent.__dict__, "configuration": {**intent.configuration, "seed": 1}}
    )
    with pytest.raises(ValidationError, match="fixed speed/correctness contract|template"):
        authorize_fast_candidate_run_prelease(drifted)


def test_fast_plan_builder_rejects_tampered_declared_artifact(tmp_path: Path) -> None:
    intent, plan, _ = prepare_campaign(tmp_path)
    task = dict(plan["tasks"][0])
    task["manifest"] = {**task["manifest"], "physical_sha256": "f" * 64}
    with pytest.raises(ValidationError, match="canonical or physical hash differs"):
        build_fast_campaign_plan(
            plan_id="tampered-plan",
            bootstrap_path=tmp_path / "bootstrap.json",
            workspace_root=tmp_path,
            tasks=[task],
            candidate_ids=["candidate-a"],
            created_at=NOW,
        )


def test_fast_status_exposes_at_most_four_independent_ready_runs(tmp_path: Path) -> None:
    intent, plan, _ = prepare_campaign(tmp_path)
    tasks = []
    for ordinal in range(5):
        task = dict(plan["tasks"][0])
        task.update(
            {
                "ordinal": ordinal,
                "task_id": f"run.B2.full-{ordinal}",
                "output_path": f"run-{ordinal}.json",
                "receipt_path": f"receipt-{ordinal}.json",
                "run_id": f"fast-campaign:run.B2.full-{ordinal}",
            }
        )
        tasks.append(task)
    parallel_plan = build_fast_campaign_plan(
        plan_id="parallel-plan",
        bootstrap_path=tmp_path / "bootstrap.json",
        workspace_root=tmp_path,
        tasks=tasks,
        candidate_ids=["candidate-a"],
        created_at=NOW,
    )
    plan_path = tmp_path / "parallel-plan.json"
    publish_immutable_fast_document(plan_path, parallel_plan)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    index_path = tmp_path / "parallel-index.json"
    publish_immutable_fast_document(index_path, index)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=tmp_path,
        generation=0,
        generated_at=NOW,
    )
    assert status["ready_tasks"] == [task["task_id"] for task in tasks[:4]]


def test_fast_bootstrap_never_calls_legacy_raw_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.benchmark.candidates as candidate_module

    def forbid_raw_replay(*_args, **_kwargs):
        raise AssertionError("fast bootstrap entered the legacy raw verifier")

    monkeypatch.setattr(
        candidate_module,
        "verify_run_raw_evidence_read_only_snapshot",
        forbid_raw_replay,
    )
    monkeypatch.setattr(
        fast_campaign_module,
        "_verify_git_revision_transition",
        lambda **_: [],
    )
    source_plan_path = tmp_path / "source-plan.json"
    source_status_path = tmp_path / "source-status.json"
    registry_path = tmp_path / "raw-registry.json"
    component_path = tmp_path / "component.bin"
    for path in (source_plan_path, source_status_path, registry_path):
        atomic_write_json(path, {"placeholder": path.stem})
    component_path.write_bytes(b"measurement-component")
    repository_root = Path(__file__).resolve().parents[3]
    oracle_documents = {}
    for relative_path in FAST_ORACLE_STATIC_ARTIFACT_PATHS.values():
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repository_root / relative_path, destination)
        oracle_documents[destination.name] = read_json(destination)
    qualified_candidate_ids = [
        row["implementation_candidate_id"]
        for row in oracle_documents["candidate-screening.v1.json"]["candidates"]
        if row["qualification_status"] == "qualified"
    ]
    task_ids = [
        "run.B1.full",
        *(f"run.B1.{candidate_id}" for candidate_id in qualified_candidate_ids),
        "run.B2.full",
    ]
    run_paths: dict[str, Path] = {}
    runs: dict[str, dict] = {}
    for task_id in task_ids:
        path = tmp_path / f"{task_id}.json"
        atomic_write_json(path, {"task_id": task_id})
        run_paths[task_id] = path
        runs[path.name] = {
            "schema_version": "run-record.v1",
            "run_id": f"source:{task_id}",
            "state": "completed",
        }
    state_root = tmp_path / "raw-state-do-not-read"
    state_root.mkdir()
    (state_root / "POISON").write_text("must not be read", encoding="utf-8")
    repository = REPOSITORY
    source_plan = {
        "campaign_id": "source",
        "repository": {
            "repo_commit": repository["commit"],
            "repo_tree": repository["tree"],
        },
        "qualified_candidate_ids": qualified_candidate_ids,
        "execution_environment_sha256": "3" * 64,
        "analyzer": {"contract": "frozen"},
        "raw_state_root": "raw-state-do-not-read",
        "tasks": [
            {
                "task_id": task_id,
                "task_type": "run",
                "stage": "B2" if task_id == "run.B2.full" else "B1",
                "kind": "candidate_empty" if task_id.endswith("full") else "single",
            }
            for task_id in task_ids
        ],
    }
    registry_rows = []
    for task_id, path in run_paths.items():
        run = runs[path.name]
        ref = artifact(tmp_path, path)
        registry_rows.append(
            {
                "task_id": task_id,
                "run_record": ref,
                "verification": {
                    "run_id": run["run_id"],
                    "run_canonical_sha256": ref["canonical_sha256"],
                    "run_physical_sha256": ref["physical_sha256"],
                    "raw_evidence_sha256": sha256_json({"task_id": task_id}),
                },
            }
        )
    registry = {
        "campaign_id": "source",
        "plan_sha256": sha256_json(source_plan),
        "raw_state_root": source_plan["raw_state_root"],
        "runs": registry_rows,
    }
    registry_ref = artifact(tmp_path, registry_path)
    source_status = {
        "campaign_id": "source",
        "plan_sha256": sha256_json(source_plan),
        "execution_environment_sha256": source_plan["execution_environment_sha256"],
        "analyzer": source_plan["analyzer"],
        "raw_evidence_registry": registry_ref,
        "tasks": [
            {
                "task_id": row["task_id"],
                "status": "completed",
                "evidence_kind": "run-record.v1",
                "evidence_path": row["run_record"]["path"],
                "evidence_sha256": row["run_record"]["canonical_sha256"],
                "evidence_physical_sha256": row["run_record"]["physical_sha256"],
            }
            for row in registry_rows
        ],
    }
    documents = {
        source_plan_path.name: source_plan,
        source_status_path.name: source_status,
        registry_path.name: registry,
        **runs,
        **oracle_documents,
    }

    def fake_load(path: Path, _version: str, *, label: str) -> dict:
        del label
        return documents[path.name]

    monkeypatch.setattr(fast_campaign_module, "_load_version", fake_load)
    result = fast_campaign_module.build_fast_bootstrap(
        bootstrap_id="bootstrap-no-replay",
        campaign_id="fast-no-replay",
        source_plan_path=source_plan_path,
        source_status_path=source_status_path,
        source_raw_registry_path=registry_path,
        source_run_paths=run_paths,
        workspace_root=tmp_path,
        evaluation_revision=repository,
        measurement_component_paths={"compiler": component_path},
        created_at=NOW,
    )
    assert len(result["imported_receipts"]) == 8
    assert (state_root / "POISON").read_text(encoding="utf-8") == "must not be read"


def test_fast_bootstrap_git_boundary_requires_clean_orchestration_only_diff(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ("git", *arguments),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.email", "fast-test@example.invalid")
    git("config", "user.name", "Fast Campaign Test")
    source_file = tmp_path / "tools/benchmark/fast_campaign.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("source = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "source")
    source = {
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": False,
    }
    source_file.write_text("source = 2\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "orchestration")
    evaluation = {
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": False,
    }
    assert fast_campaign_module._verify_git_revision_transition(
        root=tmp_path.resolve(),
        source_revision=source,
        evaluation_revision=evaluation,
    ) == ["tools/benchmark/fast_campaign.py"]

    (tmp_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="clean checked-out HEAD/tree"):
        fast_campaign_module._verify_git_revision_transition(
            root=tmp_path.resolve(),
            source_revision=source,
            evaluation_revision=evaluation,
        )
    (tmp_path / "dirty.txt").unlink()

    compiler = tmp_path / "compiler.bin"
    compiler.write_bytes(b"changed measurement component")
    git("add", ".")
    git("commit", "-m", "measurement change")
    changed_evaluation = {
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": False,
    }
    with pytest.raises(ValidationError, match="orchestration-only"):
        fast_campaign_module._verify_git_revision_transition(
            root=tmp_path.resolve(),
            source_revision=source,
            evaluation_revision=changed_evaluation,
        )


def test_fast_interrupted_receipt_accepts_only_nonresumable_infrastructure_failure() -> None:
    run = {
        "state": "interrupted",
        "completed_at": None,
        "updated_at": NOW,
        "cases": [{"cancellation_reason": "infrastructure_failure"}],
    }
    assert fast_campaign_module._terminal_summary(run) == {
        "state": "cancelled",
        "completed_at": NOW,
        "reason": "run_interrupted",
        "commitment_sha256": "0" * 64,
    }
    run["cases"][0]["cancellation_reason"] = "execution_interrupted"
    with pytest.raises(ValidationError, match="non-resumable infrastructure failure"):
        fast_campaign_module._terminal_summary(run)


def test_fast_prelease_resolves_dynamic_baseline_from_append_only_index(
    tmp_path: Path,
) -> None:
    original_intent, original_plan, baseline_run = prepare_campaign(tmp_path)
    baseline_task = dict(original_plan["tasks"][0])
    candidate_profile_path = tmp_path / "candidate-profile.json"
    atomic_write_json(candidate_profile_path, {"profile_id": "candidate-a"})
    candidate_configuration = dict(baseline_run["configuration"])
    candidate_configuration.update(
        {
            "pipeline_profile_file_sha256": sha256_file(candidate_profile_path),
            "enabled_candidate_ids": ["candidate-a"],
            "timeout_policy": "baseline_derived",
            "baseline_timeout_run_id": baseline_run["run_id"],
            "baseline_timeout_run_sha256": sha256_json(baseline_run),
        }
    )
    candidate_provenance = {
        **baseline_run["provenance"],
        "pipeline_profile_id": "candidate-a",
        "pipeline_profile_sha256": sha256_file(candidate_profile_path),
    }
    candidate_task = {
        **baseline_task,
        "ordinal": 1,
        "task_id": "run.B2.candidate-a",
        "run_kind": "single",
        "candidate_ids": ["candidate-a"],
        "dependencies": [baseline_task["task_id"]],
        "gate": "dependencies_succeeded",
        "output_path": "candidate-run.json",
        "receipt_path": "candidate-receipt.json",
        "run_id": "fast-campaign:run.B2.candidate-a",
        "logical_profile_id": "candidate-a",
        "expected_configuration_template_sha256": fast_configuration_template_sha256(
            candidate_configuration,
            candidate_provenance,
            baseline_task["task_id"],
        ),
        "baseline_task_id": baseline_task["task_id"],
        "profile": artifact(tmp_path, candidate_profile_path),
    }
    plan = build_fast_campaign_plan(
        plan_id="dynamic-baseline-plan",
        bootstrap_path=tmp_path / "bootstrap.json",
        workspace_root=tmp_path,
        tasks=[baseline_task, candidate_task],
        candidate_ids=["candidate-a"],
        created_at=NOW,
    )
    plan_path = tmp_path / "dynamic-plan.json"
    publish_immutable_fast_document(plan_path, plan)
    empty_index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    empty_index_path = tmp_path / "dynamic-index-0.json"
    publish_immutable_fast_document(empty_index_path, empty_index)
    initial_status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=empty_index_path,
        workspace_root=tmp_path,
        generation=0,
        generated_at=NOW,
    )
    initial_status_path = tmp_path / "dynamic-status-0.json"
    publish_immutable_fast_document(initial_status_path, initial_status)
    baseline_intent = FastRunAuthorizationIntent(
        **{
            **original_intent.__dict__,
            "plan_path": plan_path,
            "status_path": initial_status_path,
            "index_path": empty_index_path,
        }
    )
    receipt_path = tmp_path / "receipt.json"
    publish_fast_run_receipt(
        intent=baseline_intent,
        run_record_path=tmp_path / "run.json",
        receipt_output_path=receipt_path,
    )
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[receipt_path],
        workspace_root=tmp_path,
        previous_index_path=empty_index_path,
        generated_at=NOW,
    )
    index_path = tmp_path / "dynamic-index-1.json"
    publish_immutable_fast_document(index_path, index)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=tmp_path,
        generation=1,
        generated_at=NOW,
    )
    status_path = tmp_path / "dynamic-status-1.json"
    publish_immutable_fast_document(status_path, status)
    candidate_intent = FastRunAuthorizationIntent(
        plan_path=plan_path,
        status_path=status_path,
        index_path=index_path,
        task_id=candidate_task["task_id"],
        workspace_root=tmp_path,
        manifest_path=original_intent.manifest_path,
        output_path=tmp_path / "candidate-run.json",
        compiler_artifact_path=original_intent.compiler_artifact_path,
        pipeline_profile_path=candidate_profile_path,
        measurement_protocol_path=original_intent.measurement_protocol_path,
        baseline_run_path=tmp_path / "run.json",
        configuration=candidate_configuration,
        provenance=candidate_provenance,
        run_id=candidate_task["run_id"],
        receipt_path=tmp_path / "candidate-receipt.json",
    )
    assert authorize_fast_candidate_run_prelease(candidate_intent)["baseline_task_id"] == "run.B2.full"
    drifted = FastRunAuthorizationIntent(
        **{
            **candidate_intent.__dict__,
            "configuration": {
                **candidate_configuration,
                "baseline_timeout_run_sha256": "f" * 64,
            },
        }
    )
    with pytest.raises(ValidationError, match="exact baseline"):
        authorize_fast_candidate_run_prelease(drifted)


def test_fast_status_cancels_nonpromoted_validation_tasks_with_b3_evidence(
    tmp_path: Path,
) -> None:
    _, source_plan, source_run = prepare_campaign(tmp_path)
    template = source_plan["tasks"][0]
    static = template["static_bindings"]

    imported_refs: dict[str, dict] = {}
    imported_rows: list[dict] = []
    for ordinal, candidate_id in enumerate(("full", "candidate-a", "candidate-b")):
        task_id = f"source.B3.{candidate_id}"
        run_id = f"source:B3:{candidate_id}"
        run_path = tmp_path / f"source-{candidate_id}.json"
        imported_run = {**source_run, "run_id": run_id}
        validate_document(imported_run)
        atomic_write_json(run_path, imported_run)
        run_artifact = artifact(tmp_path, run_path)
        terminal_commitment = sha256_json(
            {"run_id": run_id, "state": imported_run["state"]}
        )
        imported_row = {
            "task_id": task_id,
            "run_id": run_id,
            "run_artifact": run_artifact,
            "terminal_commitment_sha256": terminal_commitment,
        }
        imported_row["verification_commitment_sha256"] = sha256_json(imported_row)
        imported_rows.append(imported_row)
        imported_refs[candidate_id] = {
            "ordinal": ordinal,
            "task_id": task_id,
            "run_id": run_id,
            "receipt": run_artifact,
            "terminal_commitment_sha256": terminal_commitment,
        }

    promotion_bootstrap = read_json(tmp_path / "bootstrap.json")
    promotion_bootstrap["bootstrap_id"] = "promotion-bootstrap"
    promotion_bootstrap["imported_receipts"] = imported_rows
    promotion_bootstrap = committed(
        promotion_bootstrap, "bootstrap_commitment_sha256"
    )
    promotion_bootstrap_path = tmp_path / "promotion-bootstrap.json"
    publish_immutable_fast_document(promotion_bootstrap_path, promotion_bootstrap)

    def pseudo(ordinal: int, task_id: str, stage: str, *, dependencies=(), terminal=()):
        return {
            "ordinal": ordinal,
            "task_id": task_id,
            "kind": "study",
            "run_kind": None,
            "stage": stage,
            "candidate_ids": ["candidate-a", "candidate-b"],
            "data_role": stage,
            "measurement_mode": "none",
            "dependencies": list(dependencies),
            "terminal_dependencies": list(terminal),
            "gate": "dependencies_terminal" if terminal else "always",
            "static_bindings": static,
            "output_path": f"{task_id}.json",
            "receipt_path": None,
            "run_id": None,
            "logical_profile_id": None,
            "reference_profile_id": None,
            "reference_profile_sha256": None,
            "expected_configuration_template_sha256": None,
            "baseline_task_id": None,
            "baseline_artifact": None,
            "ranking_evidence": False,
            "suite_id": None,
            "expected_case_count": None,
            "manifest": None,
            "profile": None,
            "measurement_protocol": None,
            "compiler_artifact": None,
            "execution_environment_sha256": None,
        }

    study_b3_task = pseudo(0, "study.B3", "B3")
    full = {
        **template,
        "ordinal": 1,
        "task_id": "run.B4.full",
        "stage": "B4",
        "data_role": "B4",
        "dependencies": ["study.B3"],
        "output_path": "run-B4-full.json",
        "receipt_path": "receipt-B4-full.json",
        "run_id": "fast-campaign:run.B4.full",
        "suite_id": "suite-B4",
    }
    candidate_tasks = []
    for ordinal, candidate_id in enumerate(("candidate-a", "candidate-b"), start=2):
        candidate_tasks.append(
            {
                **full,
                "ordinal": ordinal,
                "task_id": f"run.B4.{candidate_id}",
                "run_kind": "single",
                "candidate_ids": [candidate_id],
                "dependencies": [full["task_id"]],
                "gate": "candidate_eligible",
                "output_path": f"run-B4-{candidate_id}.json",
                "receipt_path": f"receipt-B4-{candidate_id}.json",
                "run_id": f"fast-campaign:run.B4.{candidate_id}",
                "logical_profile_id": candidate_id,
                "baseline_task_id": full["task_id"],
                "ranking_evidence": True,
            }
        )
    study_b4_task = pseudo(
        4,
        "study.B4",
        "B4",
        dependencies=(full["task_id"],),
        terminal=tuple(task["task_id"] for task in candidate_tasks),
    )
    plan = build_fast_campaign_plan(
        plan_id="promotion-plan",
        bootstrap_path=promotion_bootstrap_path,
        workspace_root=tmp_path,
        tasks=[study_b3_task, full, *candidate_tasks, study_b4_task],
        candidate_ids=["candidate-a", "candidate-b"],
        created_at=NOW,
    )
    plan_path = tmp_path / "promotion-plan.json"
    publish_immutable_fast_document(plan_path, plan)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    index_path = tmp_path / "promotion-index.json"
    publish_immutable_fast_document(index_path, index)

    study = committed(
        {
            "schema_version": "candidate-fast-study.v1",
            "study_id": "fast-campaign:study:B3",
            "campaign_id": "fast-campaign",
            "generated_at": NOW,
            "stage": "B3",
            "bootstrap_sha256": plan["bootstrap"]["canonical_sha256"],
            "plan_sha256": sha256_json(plan),
            "index_sha256": sha256_json(index),
            "baseline": imported_refs["full"],
            "primary_metric_id": "dynamic_instruction_count",
            "metric_unit": "instructions",
            "planned_candidate_ids": ["candidate-a", "candidate-b"],
            "evaluated_candidate_ids": ["candidate-a", "candidate-b"],
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "receipt": imported_refs[candidate_id],
                    "correctness_passed": True,
                    "metrics_complete": True,
                    "comparable_case_count": 1,
                    "geometric_mean_speedup": speedup,
                    "eligible": True,
                    "ineligibility_reason": None,
                    "per_cases": [
                        {"case_id": "case", "weight": 1.0, "speedup": speedup}
                    ],
                    "static_text_bytes_full": 110.0,
                    "static_text_bytes_full_plus_candidate": 100.0,
                    "static_text_ratio": 1.1,
                }
                for candidate_id, speedup in (
                    ("candidate-a", 1.1),
                    ("candidate-b", 0.9),
                )
            ],
            "study_commitment_sha256": "0" * 64,
        },
        "study_commitment_sha256",
    )
    study_path = tmp_path / "study-B3.json"
    publish_immutable_fast_document(study_path, study)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=tmp_path,
        generation=1,
        study_paths=[study_path],
        generated_at=NOW,
    )
    by_id = {row["task_id"]: row for row in status["tasks"]}
    assert by_id["run.B4.candidate-b"]["state"] == "cancelled"
    assert by_id["run.B4.candidate-b"]["receipt"] == status["studies"][0]["artifact"]
    assert by_id["run.B4.candidate-a"]["state"] == "pending"
    assert status["ready_tasks"] == ["run.B4.full"]


def test_fast_status_selects_exact_b3_top3_diagnostics_and_cancels_the_rest(
    tmp_path: Path,
) -> None:
    _, source_plan, source_run = prepare_campaign(tmp_path)
    template = source_plan["tasks"][0]
    static = template["static_bindings"]
    candidate_ids = ["candidate-a", "candidate-b", "candidate-c", "candidate-d"]
    imported_refs: dict[str, dict] = {}
    imported_rows: list[dict] = []
    for ordinal, candidate_id in enumerate(["full", *candidate_ids]):
        task_id = f"source.B3.{candidate_id}"
        run_id = f"source:B3:{candidate_id}"
        run_path = tmp_path / f"diagnostic-source-{candidate_id}.json"
        imported_run = {**source_run, "run_id": run_id}
        validate_document(imported_run)
        atomic_write_json(run_path, imported_run)
        run_artifact = artifact(tmp_path, run_path)
        terminal_commitment = sha256_json(
            {"run_id": run_id, "state": imported_run["state"]}
        )
        imported_row = {
            "task_id": task_id,
            "run_id": run_id,
            "run_artifact": run_artifact,
            "terminal_commitment_sha256": terminal_commitment,
        }
        imported_row["verification_commitment_sha256"] = sha256_json(imported_row)
        imported_rows.append(imported_row)
        imported_refs[candidate_id] = {
            "ordinal": ordinal,
            "task_id": task_id,
            "run_id": run_id,
            "receipt": run_artifact,
            "terminal_commitment_sha256": terminal_commitment,
        }
    bootstrap = read_json(tmp_path / "bootstrap.json")
    bootstrap["bootstrap_id"] = "diagnostic-bootstrap"
    bootstrap["imported_receipts"] = imported_rows
    bootstrap = committed(bootstrap, "bootstrap_commitment_sha256")
    bootstrap_path = tmp_path / "diagnostic-bootstrap.json"
    publish_immutable_fast_document(bootstrap_path, bootstrap)

    def pseudo(
        ordinal: int,
        task_id: str,
        stage: str,
        *,
        dependencies=(),
        terminal_dependencies=(),
        gate="always",
        kind="study",
    ) -> dict:
        return {
            "ordinal": ordinal,
            "task_id": task_id,
            "kind": kind,
            "run_kind": None,
            "stage": stage,
            "candidate_ids": candidate_ids if stage == "B3" else [],
            "data_role": stage,
            "measurement_mode": "none",
            "dependencies": list(dependencies),
            "terminal_dependencies": list(terminal_dependencies),
            "gate": gate,
            "static_bindings": static,
            "output_path": f"{task_id}.json",
            "receipt_path": None,
            "run_id": None,
            "logical_profile_id": None,
            "reference_profile_id": None,
            "reference_profile_sha256": None,
            "expected_configuration_template_sha256": None,
            "baseline_task_id": None,
            "baseline_artifact": None,
            "ranking_evidence": False,
            "suite_id": None,
            "expected_case_count": None,
            "manifest": None,
            "profile": None,
            "measurement_protocol": None,
            "compiler_artifact": None,
            "execution_environment_sha256": None,
        }

    tasks: list[dict] = []
    full = {
        **template,
        "ordinal": 0,
        "task_id": "run.B3.full",
        "stage": "B3",
        "data_role": "B3",
        "output_path": "run-B3-full.json",
        "receipt_path": "receipt-B3-full.json",
        "run_id": "fast-campaign:run.B3.full",
        "suite_id": "suite-B3",
    }
    tasks.append(full)
    tasks.append(pseudo(1, "study.B3", "B3"))

    def diagnostic(
        task_id: str,
        selected: list[str],
        mode: str,
        dependencies: list[str],
        baseline_task_id: str | None,
        gate: str,
    ) -> dict:
        ordinal = len(tasks)
        return {
            **template,
            "ordinal": ordinal,
            "task_id": task_id,
            "kind": "diagnostic",
            "run_kind": None,
            "stage": "diagnostic",
            "candidate_ids": selected,
            "data_role": "B3",
            "measurement_mode": mode,
            "dependencies": dependencies,
            "gate": gate,
            "output_path": f"{task_id}.run.json",
            "receipt_path": f"{task_id}.receipt.json",
            "run_id": f"fast-campaign:{task_id}",
                "logical_profile_id": task_id,
                "baseline_task_id": baseline_task_id,
                "ranking_evidence": False,
                "suite_id": full["suite_id"],
                "expected_case_count": full["expected_case_count"],
                "manifest": full["manifest"],
            }

    for left, right in combinations(candidate_ids, 2):
        pair = sorted((left, right))
        tasks.append(
            diagnostic(
                f"diagnostic.pair.{'+'.join(pair)}",
                pair,
                "standard_proxy",
                ["run.B3.full", "study.B3"],
                "run.B3.full",
                "diagnostic_top3",
            )
        )
    tasks.append(
        diagnostic(
            "diagnostic.cache.full",
            [],
            "cache_hotblock",
            ["study.B3"],
            None,
            "dependencies_succeeded",
        )
    )
    for candidate_id in candidate_ids:
        tasks.append(
            diagnostic(
                f"diagnostic.cache.{candidate_id}",
                [candidate_id],
                "cache_hotblock",
                ["study.B3", "diagnostic.cache.full"],
                "diagnostic.cache.full",
                "diagnostic_top3",
            )
        )
    diagnostic_ids = [task["task_id"] for task in tasks if task["kind"] == "diagnostic"]
    tasks.append(
        pseudo(
            len(tasks),
            "study.diagnostic",
            "diagnostic",
            terminal_dependencies=diagnostic_ids,
            gate="dependencies_terminal",
        )
    )
    tasks.append(
        pseudo(
            len(tasks),
            "final",
            "final",
            dependencies=("study.diagnostic",),
            gate="final_ready",
            kind="final",
        )
    )
    plan = build_fast_campaign_plan(
        plan_id="diagnostic-plan",
        bootstrap_path=bootstrap_path,
        workspace_root=tmp_path,
        tasks=tasks,
        candidate_ids=candidate_ids,
        created_at=NOW,
    )
    plan_path = tmp_path / "diagnostic-plan.json"
    publish_immutable_fast_document(plan_path, plan)
    index = build_fast_run_index(
        plan_path=plan_path,
        receipt_paths=[],
        workspace_root=tmp_path,
        generated_at=NOW,
    )
    index_path = tmp_path / "diagnostic-index.json"
    publish_immutable_fast_document(index_path, index)
    speeds = {
        "candidate-a": 1.2,
        "candidate-b": 1.4,
        "candidate-c": 1.4,
        "candidate-d": 0.8,
    }
    study = committed(
        {
            "schema_version": "candidate-fast-study.v1",
            "study_id": "fast-campaign:study:B3",
            "campaign_id": "fast-campaign",
            "generated_at": NOW,
            "stage": "B3",
            "bootstrap_sha256": sha256_json(bootstrap),
            "plan_sha256": sha256_json(plan),
            "index_sha256": sha256_json(index),
            "baseline": imported_refs["full"],
            "primary_metric_id": "dynamic_instruction_count",
            "metric_unit": "instructions",
            "planned_candidate_ids": candidate_ids,
            "evaluated_candidate_ids": candidate_ids,
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "receipt": imported_refs[candidate_id],
                    "correctness_passed": True,
                    "metrics_complete": True,
                    "comparable_case_count": 1,
                    "geometric_mean_speedup": speeds[candidate_id],
                    "eligible": True,
                    "ineligibility_reason": None,
                    "per_cases": [
                        {
                            "case_id": "case",
                            "weight": 1.0,
                            "speedup": speeds[candidate_id],
                        }
                    ],
                    "static_text_bytes_full": 110.0,
                    "static_text_bytes_full_plus_candidate": 100.0,
                    "static_text_ratio": 1.1,
                }
                for candidate_id in candidate_ids
            ],
            "study_commitment_sha256": "0" * 64,
        },
        "study_commitment_sha256",
    )
    study_path = tmp_path / "diagnostic-study-B3.json"
    publish_immutable_fast_document(study_path, study)
    status = build_fast_campaign_status(
        plan_path=plan_path,
        index_path=index_path,
        workspace_root=tmp_path,
        generation=1,
        study_paths=[study_path],
        generated_at=NOW,
    )
    by_id = {row["task_id"]: row for row in status["tasks"]}
    assert by_id["diagnostic.cache.candidate-d"]["state"] == "cancelled"
    assert by_id["diagnostic.pair.candidate-a+candidate-d"]["state"] == "cancelled"
    assert by_id["diagnostic.pair.candidate-b+candidate-c"]["state"] == "pending"
    assert by_id["diagnostic.cache.full"]["state"] == "ready"
    assert by_id["run.B3.full"]["state"] == "ready"


def test_real_diagnostic_receipts_flow_through_final_and_terminal_status(
    tmp_path: Path,
) -> None:
    candidate_metrics = dict(
        zip(REAL_QUALIFIED_CANDIDATE_IDS, (110.0, 120.0, 130.0, 140.0, 150.0, 160.0))
    )
    top3 = list(REAL_QUALIFIED_CANDIDATE_IDS[:3])
    context = _prepare_real_diagnostic_campaign(
        tmp_path / "real-final",
        candidate_metrics=candidate_metrics,
        include_b2_and_audits=True,
    )

    def publish_candidate_waves(stage: str, metrics: dict[str, float]) -> None:
        pending = set(metrics)
        wave = 0
        while pending:
            status, status_path = _integration_status(
                context, name=f"{stage.lower()}-candidates-{wave}"
            )
            ready = {
                task_id.removeprefix(f"run.{stage}.")
                for task_id in status["ready_tasks"]
                if task_id.startswith(f"run.{stage}.")
            }
            assert ready and ready <= pending and len(ready) <= 4
            for candidate_id in sorted(ready):
                _integration_publish_run(
                    context,
                    task_id=f"run.{stage}.{candidate_id}",
                    metric_value=metrics[candidate_id],
                    status_path=status_path,
                )
            _integration_advance_index(context, f"{stage.lower()}-candidates-{wave}")
            pending -= ready
            wave += 1

    initial_status, initial_status_path = _integration_status(
        context, name="initial"
    )
    assert {"audit.bootstrap", "run.B2.full"} <= set(
        initial_status["ready_tasks"]
    )
    _integration_publish_audit(context, "bootstrap", initial_status_path)
    _integration_publish_run(
        context,
        task_id="run.B2.full",
        metric_value=100.0,
        status_path=initial_status_path,
    )
    _integration_advance_index(context, "b2-full")

    publish_candidate_waves(
        "B2", {candidate_id: 105.0 for candidate_id in context["candidate_ids"]}
    )
    _integration_publish_stage_study(context, "B2")
    _, b2_audit_status_path = _integration_status(context, name="b2-study")
    _integration_publish_audit(context, "B2", b2_audit_status_path)

    b3_full_status, b3_full_status_path = _integration_status(
        context, name="b3-full"
    )
    assert "run.B3.full" in b3_full_status["ready_tasks"]
    _integration_publish_run(
        context,
        task_id="run.B3.full",
        metric_value=100.0,
        status_path=b3_full_status_path,
    )
    _integration_advance_index(context, "b3-full")
    publish_candidate_waves("B3", candidate_metrics)
    b3_study_path = _integration_publish_stage_study(context, "B3")

    selection_status, selection_status_path = _integration_status(
        context, name="b3-selection"
    )
    by_id = {row["task_id"]: row for row in selection_status["tasks"]}
    excluded = REAL_QUALIFIED_CANDIDATE_IDS[-1]
    assert by_id[f"diagnostic.cache.{excluded}"]["state"] == "cancelled"
    excluded_pair = f"diagnostic.pair.{'+'.join(sorted((top3[0], excluded)))}"
    assert by_id[excluded_pair]["state"] == "cancelled"
    _integration_publish_audit(context, "B3", selection_status_path)

    diagnostic_status, diagnostic_status_path = _integration_status(
        context, name="diagnostic-pairs"
    )
    selected_pairs = {
        f"diagnostic.pair.{'+'.join(sorted(pair))}"
        for pair in combinations(top3, 2)
    }
    assert set(diagnostic_status["ready_tasks"]) == {
        *selected_pairs,
        "diagnostic.cache.full",
    }
    for index, task_id in enumerate(sorted(selected_pairs)):
        _integration_publish_run(
            context,
            task_id=task_id,
            metric_value=90.0 + index,
            status_path=diagnostic_status_path,
        )
    _integration_publish_run(
        context,
        task_id="diagnostic.cache.full",
        metric_value=100.0,
        status_path=diagnostic_status_path,
    )
    _integration_advance_index(context, "diagnostic-pairs")

    cache_status, cache_status_path = _integration_status(
        context, name="diagnostic-cache"
    )
    selected_cache = {f"diagnostic.cache.{candidate_id}" for candidate_id in top3}
    assert set(cache_status["ready_tasks"]) == selected_cache
    for task_id in sorted(selected_cache):
        _integration_publish_run(
            context,
            task_id=task_id,
            metric_value=95.0,
            status_path=cache_status_path,
        )
    _integration_advance_index(context, "diagnostic-cache")

    waiting_status, _ = _integration_status(context, name="diagnostic-waiting")
    waiting_by_id = {row["task_id"]: row for row in waiting_status["tasks"]}
    assert waiting_by_id["study.diagnostic"]["state"] == "ready"
    assert waiting_by_id["final"]["state"] == "pending"
    pair_paths = {
        task_id: context["receipt_paths"][task_id]
        for task_id in sorted(selected_pairs)
    }
    wrong_pair_paths = dict(pair_paths)
    selected_pair_ids = sorted(selected_pairs)
    wrong_pair_paths[selected_pair_ids[0]] = context["receipt_paths"][
        selected_pair_ids[1]
    ]
    with pytest.raises(ValidationError, match="exact plan/index binding"):
        build_fast_diagnostic_study(
            bootstrap_path=context["bootstrap_path"],
            plan_path=context["plan_path"],
            index_path=context["index_path"],
            b3_study_path=b3_study_path,
            pair_receipt_paths=wrong_pair_paths,
            cache_full_receipt_path=context["receipt_paths"]["diagnostic.cache.full"],
            cache_candidate_receipt_paths={
                candidate_id: context["receipt_paths"][
                    f"diagnostic.cache.{candidate_id}"
                ]
                for candidate_id in top3
            },
            workspace_root=context["root"],
            generated_at=NOW,
        )

    diagnostic_study = build_fast_diagnostic_study(
        bootstrap_path=context["bootstrap_path"],
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        b3_study_path=b3_study_path,
        pair_receipt_paths=pair_paths,
        cache_full_receipt_path=context["receipt_paths"]["diagnostic.cache.full"],
        cache_candidate_receipt_paths={
            candidate_id: context["receipt_paths"][f"diagnostic.cache.{candidate_id}"]
            for candidate_id in top3
        },
        workspace_root=context["root"],
        generated_at=NOW,
    )
    assert diagnostic_study["top3_candidate_ids"] == top3
    assert [row["candidate_ids"] for row in diagnostic_study["pairs"]] == [
        sorted(pair) for pair in combinations(top3, 2)
    ]
    indexed_refs = read_json(context["index_path"])["receipts"]
    assert all(
        row["receipt"] in indexed_refs
        for row in [
            *diagnostic_study["pairs"],
            diagnostic_study["cache_full"],
            *diagnostic_study["cache_candidates"],
        ]
    )
    diagnostic_study_path = context["root"] / "study-diagnostic.json"
    publish_immutable_fast_document(diagnostic_study_path, diagnostic_study)

    preaudit_status, preaudit_status_path = _integration_status(
        context,
        name="preaudit",
        diagnostic_study_path=diagnostic_study_path,
    )
    assert "audit.final" in preaudit_status["ready_tasks"]
    assert "final" not in preaudit_status["ready_tasks"]
    assert preaudit_status["diagnostic_study"] == artifact(
        context["root"], diagnostic_study_path
    )
    _integration_publish_audit(context, "final", preaudit_status_path)
    final_ready_status, final_ready_status_path = _integration_status(
        context,
        name="final-ready",
        diagnostic_study_path=diagnostic_study_path,
    )
    assert "final" in final_ready_status["ready_tasks"]
    assert "audit.final" not in final_ready_status["ready_tasks"]
    report_manifest = build_fast_report(
        bootstrap_path=context["bootstrap_path"],
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        status_path=final_ready_status_path,
        audit_paths=context["audit_paths"],
        study_paths=context["study_paths"],
        diagnostic_study_path=diagnostic_study_path,
        output_directory=Path("report"),
        workspace_root=context["root"],
    )
    report_manifest_path = context["root"] / "report" / "manifest.json"
    assert report_manifest["ranking"] == []
    diagnostic_receipts = [
        context["receipt_paths"][task_id]
        for task_id in [
            *sorted(selected_pairs),
            "diagnostic.cache.full",
            *sorted(selected_cache),
        ]
    ]
    final = build_fast_final(
        bootstrap_path=context["bootstrap_path"],
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        status_path=final_ready_status_path,
        audit_paths=context["audit_paths"],
        study_paths=context["study_paths"],
        diagnostic_paths=diagnostic_receipts,
        diagnostic_study_path=diagnostic_study_path,
        report_manifest_path=report_manifest_path,
        workspace_root=context["root"],
        generated_at=NOW,
    )
    assert final["diagnostic_study"] == artifact(
        context["root"], diagnostic_study_path
    )
    assert final["promoted_candidate_ids"] == []
    final_path = context["root"] / "final.json"
    publish_immutable_fast_document(final_path, final)
    terminal_status, _ = _integration_status(
        context,
        name="terminal",
        diagnostic_study_path=diagnostic_study_path,
        final_path=final_path,
    )
    assert terminal_status["state"] == "complete"
    assert all(
        row["state"] in {"completed", "failed", "cancelled"}
        for row in terminal_status["tasks"]
    )


def test_real_diagnostic_study_allows_empty_pair_set_below_top2(
    tmp_path: Path,
) -> None:
    context = _prepare_real_diagnostic_campaign(
        tmp_path / "real-single",
        candidate_metrics={"candidate-a": 110.0},
        include_b2_and_audits=False,
    )
    _, full_status_path = _integration_status(context, name="b3-full")
    _integration_publish_run(
        context,
        task_id="run.B3.full",
        metric_value=100.0,
        status_path=full_status_path,
    )
    _integration_advance_index(context, "b3-full")
    _, candidate_status_path = _integration_status(context, name="b3-candidate")
    _integration_publish_run(
        context,
        task_id="run.B3.candidate-a",
        metric_value=110.0,
        status_path=candidate_status_path,
    )
    _integration_advance_index(context, "b3-candidate")
    b3_study_path = _integration_publish_stage_study(context, "B3")
    _, cache_full_status_path = _integration_status(context, name="cache-full")
    _integration_publish_run(
        context,
        task_id="diagnostic.cache.full",
        metric_value=100.0,
        status_path=cache_full_status_path,
    )
    _integration_advance_index(context, "cache-full")
    _, cache_status_path = _integration_status(context, name="cache-candidate")
    _integration_publish_run(
        context,
        task_id="diagnostic.cache.candidate-a",
        metric_value=95.0,
        status_path=cache_status_path,
    )
    _integration_advance_index(context, "cache-candidate")
    diagnostic_study = build_fast_diagnostic_study(
        bootstrap_path=context["bootstrap_path"],
        plan_path=context["plan_path"],
        index_path=context["index_path"],
        b3_study_path=b3_study_path,
        pair_receipt_paths={},
        cache_full_receipt_path=context["receipt_paths"]["diagnostic.cache.full"],
        cache_candidate_receipt_paths={
            "candidate-a": context["receipt_paths"]["diagnostic.cache.candidate-a"]
        },
        workspace_root=context["root"],
        generated_at=NOW,
    )
    assert diagnostic_study["top3_candidate_ids"] == ["candidate-a"]
    assert diagnostic_study["pairs"] == []
