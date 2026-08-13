from __future__ import annotations

from copy import deepcopy

import pytest

from tools.benchmark.errors import ValidationError
from tools.benchmark.schema import schema_sha256, validate_document
from tools.benchmark.util import sha256_json


_NOW = "2026-08-13T00:00:00Z"
_COMMIT = "1" * 40
_TREE = "2" * 40
_ORACLE_STATIC_IDS = (
    "candidate-screening",
    "candidate-oracle-capture",
    "candidate-evidence",
    "candidate-screening-spec",
)


def _artifact(name: str, digit: str = "a") -> dict:
    return {
        "path": f".tmp/fast/{name}",
        "canonical_sha256": digit * 64,
        "physical_sha256": digit * 64,
    }


def _repository() -> dict:
    return {"commit": _COMMIT, "tree": _TREE, "dirty": False}


def _commit(document: dict, field: str) -> dict:
    document[field] = sha256_json(
        {key: value for key, value in document.items() if key != field}
    )
    return document


def _named_artifact(name: str, digit: str = "a", *, verified: bool = False) -> dict:
    row = {"artifact_id": name, "artifact": _artifact(f"{name}.json", digit)}
    if verified:
        row["verification_commitment_sha256"] = sha256_json(row)
    return row


def _oracle_static(*, verified: bool = False) -> list[dict]:
    return [
        _named_artifact(artifact_id, str(index + 5), verified=verified)
        for index, artifact_id in enumerate(_ORACLE_STATIC_IDS)
    ]


def _bootstrap() -> dict:
    imported = {
        "task_id": "run.B1.full",
        "run_id": "prior:run.B1.full",
        "run_artifact": _artifact("prior-b1.json", "d"),
        "terminal_commitment_sha256": "e" * 64,
    }
    imported["verification_commitment_sha256"] = sha256_json(imported)
    return _commit(
        {
            "schema_version": "candidate-fast-bootstrap.v1",
            "bootstrap_id": "fast-bootstrap",
            "campaign_id": "fast-campaign",
            "created_at": _NOW,
            "source_revision": _repository(),
            "evaluation_revision": _repository(),
            "source_artifacts": [_named_artifact("source-plan", "3", verified=True)],
            "static_artifacts": [
                *_oracle_static(verified=True),
                _named_artifact("run-schema", "4", verified=True),
            ],
            "imported_receipts": [imported],
            "bootstrap_commitment_sha256": "0" * 64,
        },
        "bootstrap_commitment_sha256",
    )


def _task(ordinal: int, task_id: str, *, dependencies: list[str] | None = None) -> dict:
    dependencies = dependencies or []
    run_kind = "single" if dependencies else "candidate_empty"
    return {
        "ordinal": ordinal,
        "task_id": task_id,
        "kind": "run",
        "run_kind": run_kind,
        "stage": "B2",
        "candidate_ids": ["candidate-a"] if run_kind == "single" else [],
        "data_role": "B2",
        "measurement_mode": "standard_proxy",
        "dependencies": dependencies,
        "terminal_dependencies": [],
        "gate": "dependencies_succeeded" if dependencies else "always",
        "static_bindings": [
            *_oracle_static(),
            _named_artifact(f"manifest-{ordinal}", "5"),
        ],
        "output_path": f".tmp/fast/run-{ordinal}.json",
        "receipt_path": f".tmp/fast/receipt-{ordinal}.json",
        "run_id": f"fast-campaign:{task_id}",
        "logical_profile_id": "full+candidate-a",
        "reference_profile_id": None,
        "reference_profile_sha256": None,
        "expected_configuration_template_sha256": "c" * 64,
        "baseline_task_id": dependencies[-1] if dependencies else None,
        "baseline_artifact": None,
        "ranking_evidence": False,
        "suite_id": "suite-B2",
        "expected_case_count": 20,
        "manifest": _artifact("manifest-B2.json", "d"),
        "profile": _artifact("profile-candidate-a.json", "e"),
        "measurement_protocol": _artifact("standard-protocol.json", "f"),
        "compiler_artifact": _artifact("compiler.bin", "1"),
        "execution_environment_sha256": "2" * 64,
    }


def _plan(task_count: int = 2) -> dict:
    tasks = []
    for ordinal in range(task_count):
        task_id = f"run.B2.candidate-{ordinal}"
        tasks.append(
            _task(
                ordinal,
                task_id,
                dependencies=[] if ordinal == 0 else [tasks[ordinal - 1]["task_id"]],
            )
        )
    return _commit(
        {
            "schema_version": "candidate-fast-campaign-plan.v1",
            "plan_id": "fast-plan",
            "campaign_id": "fast-campaign",
            "created_at": _NOW,
            "bootstrap_id": "fast-bootstrap",
            "bootstrap": _artifact("bootstrap.json", "6"),
            "repository": _repository(),
            "max_parallel_runs": 4,
            "jobs_per_run": 4,
            "candidate_ids": ["candidate-a"],
            "tasks": tasks,
            "plan_commitment_sha256": "0" * 64,
        },
        "plan_commitment_sha256",
    )


def _receipt(ordinal: int = 0, task_id: str = "run.B2.candidate-0") -> dict:
    configuration = {
        "configuration_sha256": "0" * 64,
        "repository": _repository(),
        "compiler_artifact_sha256": "1" * 64,
        "execution_environment_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
        "profile_sha256": "4" * 64,
        "protocol_sha256": "5" * 64,
        "jobs": 4,
    }
    configuration["configuration_sha256"] = sha256_json(
        {key: value for key, value in configuration.items() if key != "configuration_sha256"}
    )
    receipt = {
        "schema_version": "candidate-fast-run-receipt.v1",
        "receipt_id": f"receipt-{ordinal}",
        "campaign_id": "fast-campaign",
        "bootstrap_sha256": "6" * 64,
        "plan_sha256": "7" * 64,
        "ordinal": ordinal,
        "task_id": task_id,
        "run_id": f"fast:{task_id}",
        "stage": "B2",
        "candidate_ids": ["candidate-a"],
        "configuration": configuration,
        "run_artifact": _artifact(f"run-{ordinal}.json", "8"),
        "correctness": {
            "expected_cases": 2,
            "passed_cases": 2,
            "failed_cases": 0,
            "timed_out_cases": 0,
            "pending_cases": 0,
            "all_correct": True,
        },
        "metrics": {
            "primary_metric_id": "dynamic_instruction_count",
            "unit": "instructions",
            "sample_count": 2,
            "aggregate_sha256": "9" * 64,
            "complete": True,
        },
        "terminal": {
            "state": "completed",
            "completed_at": _NOW,
            "reason": None,
            "commitment_sha256": "0" * 64,
        },
    }
    terminal = dict(receipt["terminal"])
    terminal.pop("commitment_sha256")
    payload = dict(receipt)
    payload["terminal"] = terminal
    receipt["terminal"]["commitment_sha256"] = sha256_json(payload)
    return receipt


def _receipt_ref(ordinal: int = 0, task_id: str = "run.B2.candidate-0") -> dict:
    return {
        "ordinal": ordinal,
        "task_id": task_id,
        "run_id": f"fast:{task_id}",
        "receipt": _artifact(f"receipt-{ordinal}.json", "a"),
        "terminal_commitment_sha256": "b" * 64,
    }


def _index(receipts: list[dict] | None = None) -> dict:
    return _commit(
        {
            "schema_version": "candidate-fast-run-index.v1",
            "index_id": "fast-index-1",
            "campaign_id": "fast-campaign",
            "generated_at": _NOW,
            "bootstrap_sha256": "6" * 64,
            "plan_sha256": "7" * 64,
            "receipts": receipts if receipts is not None else [_receipt_ref()],
            "index_commitment_sha256": "0" * 64,
        },
        "index_commitment_sha256",
    )


def _status(task_count: int = 4) -> dict:
    tasks = [
        {
            "ordinal": ordinal,
            "task_id": f"run.B2.candidate-{ordinal}",
            "stage": "B2",
            "state": "ready",
            "receipt": None,
            "terminal_commitment_sha256": None,
            "blocked_by": [],
        }
        for ordinal in range(task_count)
    ]
    return _commit(
        {
            "schema_version": "candidate-fast-status.v1",
            "status_id": "fast-status-1",
            "campaign_id": "fast-campaign",
            "generation": 1,
            "generated_at": _NOW,
            "bootstrap": _artifact("bootstrap.json", "6"),
            "plan": _artifact("plan.json", "7"),
            "index": _artifact("index.json", "8"),
            "state": "running",
            "tasks": tasks,
            "ready_tasks": [task["task_id"] for task in tasks],
            "studies": [],
            "audits": [],
            "diagnostics": [],
            "diagnostic_study": None,
            "final": None,
            "status_commitment_sha256": "0" * 64,
        },
        "status_commitment_sha256",
    )


def _head() -> dict:
    return _commit(
        {
            "schema_version": "candidate-fast-current-head.v1",
            "campaign_id": "fast-campaign",
            "generation": 1,
            "updated_at": _NOW,
            "bootstrap_sha256": "6" * 64,
            "plan_sha256": "7" * 64,
            "status_id": "fast-status-1",
            "status": _artifact("status.json", "8"),
            "index_id": "fast-index-1",
            "index": _artifact("index.json", "9"),
            "head_commitment_sha256": "0" * 64,
        },
        "head_commitment_sha256",
    )


def _audit() -> dict:
    return _commit(
        {
            "schema_version": "candidate-fast-audit.v1",
            "audit_id": "audit-bootstrap",
            "campaign_id": "fast-campaign",
            "generated_at": _NOW,
            "checkpoint": "bootstrap",
            "bootstrap_sha256": "6" * 64,
            "plan_sha256": "7" * 64,
            "index_sha256": "8" * 64,
            "status": _artifact("status.json", "9"),
            "status_sha256": "9" * 64,
            "scope_receipts": [],
            "checks": [
                {"check_id": "identities", "outcome": "passed", "details_sha256": "a" * 64}
            ],
            "passed": True,
            "audit_commitment_sha256": "0" * 64,
        },
        "audit_commitment_sha256",
    )


def _study() -> dict:
    return _commit(
        {
            "schema_version": "candidate-fast-study.v1",
            "study_id": "study-B2",
            "campaign_id": "fast-campaign",
            "generated_at": _NOW,
            "stage": "B2",
            "bootstrap_sha256": "6" * 64,
            "plan_sha256": "7" * 64,
            "index_sha256": "8" * 64,
            "baseline": _receipt_ref(0, "run.B2.full"),
            "primary_metric_id": "dynamic_instruction_count",
            "metric_unit": "instructions",
            "planned_candidate_ids": ["candidate-a"],
            "evaluated_candidate_ids": ["candidate-a"],
            "candidates": [
                {
                    "candidate_id": "candidate-a",
                    "receipt": _receipt_ref(1, "run.B2.candidate-a"),
                    "correctness_passed": True,
                    "metrics_complete": True,
                    "comparable_case_count": 1,
                    "geometric_mean_speedup": 1.1,
                    "eligible": True,
                    "ineligibility_reason": None,
                    "per_cases": [
                        {"case_id": "case-1", "weight": 1.0, "speedup": 1.1}
                    ],
                    "static_text_bytes_full": 110.0,
                    "static_text_bytes_full_plus_candidate": 100.0,
                    "static_text_ratio": 1.1,
                }
            ],
            "study_commitment_sha256": "0" * 64,
        },
        "study_commitment_sha256",
    )


def _stage_result(stage: str) -> dict:
    return {
        "study_sha256": (str((len(stage) % 9) + 1)) * 64,
        "receipt_sha256": "a" * 64,
        "eligible": True,
        "geometric_mean_speedup": 1.1,
    }


def _diagnostic_study() -> dict:
    return _commit(
        {
            "schema_version": "candidate-fast-diagnostic-study.v1",
            "diagnostic_study_id": "fast-campaign:study:diagnostic",
            "campaign_id": "fast-campaign",
            "generated_at": _NOW,
            "bootstrap_sha256": "6" * 64,
            "plan_sha256": "7" * 64,
            "index_sha256": "8" * 64,
            "b3_study": _artifact("study-B3.json", "9"),
            "top3_candidate_ids": ["candidate-a"],
            "pairs": [],
            "cache_full": {
                "receipt": _receipt_ref(2, "diagnostic.cache.full"),
                "terminal_state": "completed",
                "correctness_passed": True,
                "metrics_complete": True,
            },
            "cache_candidates": [
                {
                    "candidate_id": "candidate-a",
                    "receipt": _receipt_ref(3, "diagnostic.cache.candidate-a"),
                    "terminal_state": "completed",
                    "correctness_passed": True,
                    "metrics_complete": True,
                }
            ],
            "diagnostic_study_commitment_sha256": "0" * 64,
        },
        "diagnostic_study_commitment_sha256",
    )


def _final() -> dict:
    candidate = {
        "candidate_id": "candidate-a",
        "stages": {stage: _stage_result(stage) for stage in ("B2", "B3", "B4", "B5", "B6")},
        "eligible_for_final": True,
        "ineligibility_reasons": [],
        "combined_case_count": 267,
        "combined_geometric_mean_speedup": 1.1,
        "b3_geometric_mean_speedup": 1.1,
        "combined_static_text_bytes_full_plus_candidate": 100.0,
        "combined_static_text_ratio": 1.1,
        "rank": 1,
    }
    return _commit(
        {
            "schema_version": "candidate-fast-final.v1",
            "final_id": "fast-final",
            "campaign_id": "fast-campaign",
            "generated_at": _NOW,
            "evidence_level": "qemu_proxy",
            "bootstrap": _artifact("bootstrap.json", "1"),
            "plan": _artifact("plan.json", "2"),
            "index": _artifact("index.json", "3"),
            "status": _artifact("status.json", "4"),
            "audits": {
                checkpoint: _artifact(f"audit-{checkpoint}.json", "5")
                for checkpoint in ("bootstrap", "B2", "B3", "final")
            },
            "studies": {
                stage: _artifact(f"study-{stage}.json", "6")
                for stage in ("B2", "B3", "B4", "B5", "B6")
            },
            "diagnostics": [],
            "diagnostic_study": _artifact("diagnostic-study.json", "a"),
            "planned_candidate_ids": ["candidate-a"],
            "promoted_candidate_ids": ["candidate-a"],
            "candidates": [candidate],
            "ranking": [
                {
                    "rank": 1,
                    "candidate_id": "candidate-a",
                    "combined_geometric_mean_speedup": 1.1,
                    "b3_geometric_mean_speedup": 1.1,
                    "combined_static_text_bytes_full_plus_candidate": 100.0,
                    "combined_static_text_ratio": 1.1,
                    "stable_id_tiebreak": "candidate-a",
                }
            ],
            "winner_candidate_id": "candidate-a",
            "report_manifest": _artifact("report-manifest.json", "b"),
            "report_artifacts": {
                "report": _artifact("report.md", "7"),
                "single_candidate_chart": _artifact("single-candidate.svg", "8"),
                "ranking_chart": _artifact("ranking.svg", "8"),
                "suite_chart": _artifact("suites.svg", "9"),
                "pair_heatmap": _artifact("pair-heatmap.svg", "a"),
                "oracle_capture_chart": _artifact("oracle-capture.svg", "b"),
                "cache_hotblock_chart": _artifact("cache-hotblock.svg", "c"),
                "pareto_chart": _artifact("pareto.svg", "d"),
            },
            "final_commitment_sha256": "0" * 64,
        },
        "final_commitment_sha256",
    )


@pytest.mark.parametrize(
    "document",
    [
        _bootstrap(),
        _plan(),
        _receipt(),
        _index(),
        _status(),
        _head(),
        _audit(),
        _study(),
        _diagnostic_study(),
        _final(),
    ],
)
def test_fast_campaign_schema_accepts_valid_documents(document: dict) -> None:
    assert validate_document(document) is document
    assert len(schema_sha256(document["schema_version"])) == 64


def test_fast_schemas_reject_unknown_properties_and_tampered_commitments() -> None:
    bootstrap = _bootstrap()
    bootstrap["unexpected"] = True
    with pytest.raises(ValidationError, match="Additional properties"):
        validate_document(bootstrap)

    plan = _plan()
    plan["tasks"][0]["output_path"] = ".tmp/fast/tampered.json"
    with pytest.raises(ValidationError, match="commitment differs"):
        validate_document(plan)


def test_fast_final_requires_the_complete_report_artifact_set() -> None:
    final = _final()
    final["report_artifacts"].pop("pair_heatmap")
    _commit(final, "final_commitment_sha256")
    with pytest.raises(ValidationError, match="pair_heatmap.*required"):
        validate_document(final)


def test_fast_plan_locks_parallelism_and_rejects_forward_dependencies() -> None:
    plan = _plan()
    plan["max_parallel_runs"] = 5
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="4 was expected"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][0]["dependencies"] = [plan["tasks"][1]["task_id"]]
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="must precede"):
        validate_document(plan)


def test_fast_plan_run_authorization_is_explicit_and_exact() -> None:
    plan = _plan()
    plan["tasks"][0]["manifest"] = None
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="lacks an exact run authorization"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][0]["run_id"] = "other-campaign:run.B2.candidate-0"
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="escapes the campaign namespace"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][1]["baseline_task_id"] = plan["tasks"][0]["task_id"]
    plan["tasks"][1]["dependencies"] = []
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="success-depend on its baseline"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][1]["manifest"] = _artifact("other-manifest.json", "3")
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="exact suite binding"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][1]["baseline_artifact"] = _artifact("run-0.json", "a")
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="resolve dynamically"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][0]["compiler_artifact"] = _artifact("other-compiler.bin", "a")
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="exact measured contract"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][0]["baseline_artifact"] = _artifact("imported.json", "b")
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="requires a baseline task identity"):
        validate_document(plan)

    plan = _plan()
    plan["tasks"][1]["baseline_task_id"] = "imported.B2.full"
    plan["tasks"][1]["dependencies"] = []
    plan["tasks"][1]["baseline_artifact"] = _artifact("imported-B2-full.json", "b")
    _commit(plan, "plan_commitment_sha256")
    assert validate_document(plan)["tasks"][1]["baseline_artifact"] is not None


def test_fast_reference_run_uses_the_frozen_non_file_profile_identity() -> None:
    plan = _plan(1)
    task = plan["tasks"][0]
    task["run_kind"] = "reference"
    task["logical_profile_id"] = "gcc-13.3-o2"
    task["reference_profile_id"] = "gcc-13.3-o2"
    task["reference_profile_sha256"] = "c" * 64
    task["profile"] = None
    snapshot = _artifact("reference-toolchain.json", "7")
    task["static_bindings"].append(
        {"artifact_id": "reference-toolchain-snapshot", "artifact": snapshot}
    )
    task["compiler_artifact"] = snapshot
    _commit(plan, "plan_commitment_sha256")
    validated = validate_document(plan)["tasks"][0]
    assert validated["profile"] is None
    assert validated["reference_profile_sha256"] == "c" * 64

    plan["tasks"][0]["profile"] = _artifact("fake-accela-profile.json", "c")
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="reference run contract differs"):
        validate_document(plan)

    plan = _plan(1)
    task = plan["tasks"][0]
    task["run_kind"] = "reference"
    task["logical_profile_id"] = "gcc-13.3-o2"
    task["reference_profile_id"] = None
    task["reference_profile_sha256"] = "c" * 64
    task["profile"] = None
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="reference run contract differs"):
        validate_document(plan)

    plan = _plan(1)
    task = plan["tasks"][0]
    task["run_kind"] = "reference"
    task["logical_profile_id"] = "gcc-13.3-o2"
    task["reference_profile_id"] = "gcc-13.3-o2"
    task["reference_profile_sha256"] = "c" * 64
    task["profile"] = None
    _commit(plan, "plan_commitment_sha256")
    with pytest.raises(ValidationError, match="frozen toolchain snapshot"):
        validate_document(plan)


def test_fast_receipt_fails_closed_on_counts_configuration_and_terminal_binding() -> None:
    receipt = _receipt()
    receipt["correctness"]["passed_cases"] = 1
    with pytest.raises(ValidationError, match="counts must equal"):
        validate_document(receipt)

    receipt = _receipt()
    receipt["configuration"]["manifest_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="configuration digest differs"):
        validate_document(receipt)

    receipt = _receipt()
    receipt["metrics"]["aggregate_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="terminal commitment differs"):
        validate_document(receipt)


def test_fast_index_and_status_preserve_plan_order_and_four_way_readiness() -> None:
    assert len(validate_document(_status())["ready_tasks"]) == 4

    status = _status(5)
    with pytest.raises(ValidationError, match="too long"):
        validate_document(status)

    index = _index(
        [_receipt_ref(1, "run.B2.candidate-1"), _receipt_ref(0, "run.B2.candidate-0")]
    )
    with pytest.raises(ValidationError, match="plan-ordinal order"):
        validate_document(index)


def test_fast_status_can_terminally_cancel_an_unpromoted_task_with_study_evidence() -> None:
    status = _status(1)
    task = status["tasks"][0]
    task["state"] = "cancelled"
    task["receipt"] = _artifact("study-B3.json", "c")
    task["terminal_commitment_sha256"] = "d" * 64
    status["ready_tasks"] = []
    _commit(status, "status_commitment_sha256")
    assert validate_document(status)["tasks"][0]["state"] == "cancelled"


def test_fast_audit_study_and_final_derive_their_decisions() -> None:
    audit = _audit()
    audit["passed"] = False
    _commit(audit, "audit_commitment_sha256")
    with pytest.raises(ValidationError, match="passed flag differs"):
        validate_document(audit)

    study = _study()
    study["candidates"][0]["metrics_complete"] = False
    _commit(study, "study_commitment_sha256")
    with pytest.raises(ValidationError, match="eligibility differs"):
        validate_document(study)

    final = _final()
    final["candidates"][0]["stages"]["B6"]["eligible"] = False
    _commit(final, "final_commitment_sha256")
    with pytest.raises(ValidationError, match="eligibility differs"):
        validate_document(final)

    diagnostic = _diagnostic_study()
    diagnostic["cache_candidates"] = []
    _commit(diagnostic, "diagnostic_study_commitment_sha256")
    with pytest.raises(ValidationError, match="must equal Top3"):
        validate_document(diagnostic)


def test_fast_study_allows_only_promoted_subsets_after_b3() -> None:
    study = _study()
    study["planned_candidate_ids"] = ["candidate-a", "candidate-b"]
    _commit(study, "study_commitment_sha256")
    with pytest.raises(ValidationError, match="every planned candidate"):
        validate_document(study)

    study = _study()
    study["stage"] = "B4"
    study["planned_candidate_ids"] = ["candidate-a", "candidate-b"]
    _commit(study, "study_commitment_sha256")
    assert validate_document(study)["evaluated_candidate_ids"] == ["candidate-a"]

    study["evaluated_candidate_ids"] = []
    study["candidates"] = []
    _commit(study, "study_commitment_sha256")
    assert validate_document(study)["candidates"] == []


def test_fast_final_b3_threshold_is_strict_and_validation_stages_are_nullable() -> None:
    final = _final()
    final["candidates"][0]["stages"]["B3"]["geometric_mean_speedup"] = 1.0
    for stage in ("B4", "B5", "B6"):
        final["studies"][stage] = None
        final["candidates"][0]["stages"][stage] = None
    final["promoted_candidate_ids"] = []
    final["candidates"][0]["eligible_for_final"] = False
    final["candidates"][0]["ineligibility_reasons"] = ["not_promoted_by_B3"]
    final["candidates"][0]["combined_geometric_mean_speedup"] = None
    final["candidates"][0]["combined_static_text_bytes_full_plus_candidate"] = None
    final["candidates"][0]["combined_static_text_ratio"] = None
    final["candidates"][0]["rank"] = None
    final["ranking"] = []
    final["winner_candidate_id"] = None
    _commit(final, "final_commitment_sha256")
    assert validate_document(final)["promoted_candidate_ids"] == []

    final["promoted_candidate_ids"] = ["candidate-a"]
    _commit(final, "final_commitment_sha256")
    with pytest.raises(ValidationError, match="strict B3 > 1.0"):
        validate_document(final)


def test_fast_final_requires_exact_promoted_validation_coverage() -> None:
    final = _final()
    final["candidates"][0]["stages"]["B6"] = None
    _commit(final, "final_commitment_sha256")
    with pytest.raises(ValidationError, match="requires B4-B6 evidence"):
        validate_document(final)

    final = _final()
    final["promoted_candidate_ids"] = []
    _commit(final, "final_commitment_sha256")
    with pytest.raises(ValidationError, match="strict B3 > 1.0"):
        validate_document(final)


def test_fast_final_rejects_b2_ineligible_candidate_from_ranking() -> None:
    final = _final()
    final["candidates"][0]["stages"]["B2"]["eligible"] = False
    final["candidates"][0]["stages"]["B2"]["geometric_mean_speedup"] = None
    _commit(final, "final_commitment_sha256")
    with pytest.raises(ValidationError, match="eligibility differs"):
        validate_document(final)

    final["candidates"][0]["eligible_for_final"] = False
    final["candidates"][0]["ineligibility_reasons"] = ["B2:correctness_failure"]
    final["candidates"][0]["combined_geometric_mean_speedup"] = None
    final["candidates"][0]["combined_static_text_bytes_full_plus_candidate"] = None
    final["candidates"][0]["combined_static_text_ratio"] = None
    final["candidates"][0]["rank"] = None
    final["ranking"] = []
    final["winner_candidate_id"] = None
    _commit(final, "final_commitment_sha256")
    assert validate_document(final)["ranking"] == []
