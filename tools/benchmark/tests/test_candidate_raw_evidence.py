from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from tools.benchmark import candidates as candidate_module
from tools.benchmark.errors import ConfigurationError, ValidationError
from tools.benchmark.execution import VerifiedRunRawEvidence
from tools.benchmark.schema import schema_sha256, validate_document
from tools.benchmark.util import atomic_write_json, sha256_file, sha256_json


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _raw_verification(run: Mapping[str, Any], run_path: Path) -> dict[str, Any]:
    document = {
        "schema_version": "benchmark-run-raw-evidence.v1",
        "run_id": run["run_id"],
        "run_canonical_sha256": sha256_json(run),
        "run_physical_sha256": sha256_file(run_path),
        "state_tree_sha256": SHA_A,
        "terminal_observed_at": "2026-08-09T00:00:01Z",
        "terminal_journal_sha256": SHA_B,
        "terminal_journal_event_count": 1,
        "attempt_count": 1,
        "terminal_attempt_count": 1,
        "cases": [
            {
                "case_id": f"case-{run['run_id']}",
                "attempts": [
                    {
                        "attempt_index": 0,
                        "identity_sha256": SHA_B,
                        "journal_commitment_sha256": SHA_C,
                        "journal_event_count": 1,
                        "raw_files_sha256": SHA_D,
                        "remark_files_sha256": None,
                    }
                ],
                "current_attempt_index": 0,
            }
        ],
    }
    document["raw_evidence_sha256"] = sha256_json(
        {
            "schema_version": "benchmark-run-raw-evidence-commitment.v1",
            "document": document,
        }
    )
    return document


def _recommit_verification(verification: dict[str, Any]) -> None:
    document = {
        key: deepcopy(value)
        for key, value in verification.items()
        if key != "raw_evidence_sha256"
    }
    verification["raw_evidence_sha256"] = sha256_json(
        {
            "schema_version": "benchmark-run-raw-evidence-commitment.v1",
            "document": document,
        }
    )


def _rederive_registry_id(registry: dict[str, Any]) -> None:
    registry["registry_id"] = (
        f"{registry['campaign_id']}:raw:{sha256_json(registry['runs'])[:32]}"
    )


@pytest.fixture
def raw_registry_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    state_root = tmp_path / "raw-state"
    state_root.mkdir()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}\n", encoding="utf-8")
    run_paths = {
        "main.b1.full": tmp_path / "runs" / "full.json",
        "main.b1.candidate": tmp_path / "runs" / "candidate.json",
    }
    runs: dict[Path, dict[str, Any]] = {}
    for index, (task_id, path) in enumerate(run_paths.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{{"task":"{task_id}"}}\n', encoding="utf-8")
        runs[path.resolve()] = {
            "schema_version": "run-record.v1",
            "run_id": f"run-{index}",
            "configuration": {"enabled_candidate_ids": []},
            "provenance": {
                "pipeline_profile_id": "profile-full",
                "pipeline_profile_sha256": SHA_A,
            },
            "cases": [],
        }
    plan = {
        "schema_version": "candidate-campaign-plan.v1",
        "campaign_id": "campaign-raw-registry",
        "candidate_raw_evidence_schema_sha256": schema_sha256(
            "candidate-raw-evidence.v1"
        ),
        "raw_state_root": "raw-state",
        "artifacts": {
            "candidate_registry": {"path": "catalog.json"},
            "executable_pass_registry": {
                "path": "pass-registry.executable.v2.json"
            },
            "screening_base_pass_registry": {
                "path": "pass-registry.base.v2.json"
            },
            "screening": {"path": "screening.json"},
        },
        "tasks": [
            {"task_id": task_id, "task_type": "run"}
            for task_id in run_paths
        ],
    }
    catalog = {
        "schema_version": "candidate-catalog.v1",
        "candidates": [],
    }
    pass_registry = {
        "schema_version": "pass-registry.v2",
        "passes": [],
    }
    screening = {
        "schema_version": "candidate-screening.v1",
        "base_pass_registry": {
            "path": "pass-registry.base.v2.json",
            "canonical_sha256": sha256_json(pass_registry),
            "physical_sha256": SHA_D,
        },
        "pass_registry_sha256": sha256_json(pass_registry),
    }
    plan["artifacts"]["screening_base_pass_registry"] = deepcopy(
        screening["base_pass_registry"]
    )
    original_load_version = candidate_module._load_version

    def fake_load_version(path: Path, version: str, *, label: str) -> dict[str, Any]:
        if version == "candidate-campaign-plan.v1":
            return deepcopy(plan)
        if version == "run-record.v1":
            return deepcopy(runs[path.resolve()])
        return original_load_version(path, version, label=label)

    def fake_load_frozen_artifact(
        workspace_root: Path,
        artifact: Mapping[str, str],
        *,
        label: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        del workspace_root, artifact, label
        if version == "candidate-catalog.v1":
            return deepcopy(catalog)
        if version == "pass-registry.v2":
            return deepcopy(pass_registry)
        if version == "candidate-screening.v1":
            return deepcopy(screening)
        raise AssertionError(f"unexpected frozen artifact version: {version}")

    def fake_verify_candidate_run_raw_evidence(
        *,
        run_path: Path,
        state_root: Path,
        catalog: Mapping[str, Any],
        pass_registry: Mapping[str, Any],
        raw_evidence_verifier: Any | None = None,
    ) -> tuple[dict[str, Any], VerifiedRunRawEvidence]:
        del catalog, pass_registry
        del raw_evidence_verifier
        assert state_root == (tmp_path / "raw-state").resolve()
        run = deepcopy(runs[run_path.resolve()])
        return run, VerifiedRunRawEvidence(
            document=_raw_verification(run, run_path),
            current_remark_paths={},
        )

    monkeypatch.setattr(candidate_module, "_load_version", fake_load_version)
    monkeypatch.setattr(
        candidate_module, "_load_frozen_artifact", fake_load_frozen_artifact
    )
    monkeypatch.setattr(
        candidate_module,
        "_require_executable_registry_bridge",
        lambda **_: deepcopy(pass_registry),
    )
    monkeypatch.setattr(
        candidate_module,
        "_load_and_reverify_candidate_screening",
        lambda **_: deepcopy(screening),
    )
    monkeypatch.setattr(
        candidate_module,
        "_verify_candidate_run_raw_evidence",
        fake_verify_candidate_run_raw_evidence,
    )
    return {
        "workspace_root": tmp_path,
        "plan_path": plan_path,
        "plan": plan,
        "run_paths": run_paths,
    }


def _build_registry(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return candidate_module.build_candidate_raw_evidence_registry(
        plan_path=fixture["plan_path"],
        run_paths=fixture["run_paths"],
        workspace_root=fixture["workspace_root"],
    )


def test_candidate_raw_registry_is_deterministic_and_task_ordered(
    raw_registry_fixture: Mapping[str, Any],
) -> None:
    first = _build_registry(raw_registry_fixture)
    reversed_fixture = dict(raw_registry_fixture)
    reversed_fixture["run_paths"] = dict(
        reversed(tuple(raw_registry_fixture["run_paths"].items()))
    )
    second = _build_registry(reversed_fixture)

    assert first == second
    assert [item["task_id"] for item in first["runs"]] == sorted(
        raw_registry_fixture["run_paths"]
    )
    assert validate_document(first) == first


def test_candidate_raw_registry_rejects_unknown_task(
    raw_registry_fixture: Mapping[str, Any],
) -> None:
    run_paths = dict(raw_registry_fixture["run_paths"])
    run_paths["unknown.task"] = next(iter(run_paths.values()))

    with pytest.raises(ConfigurationError, match="unknown candidate raw run task"):
        candidate_module.build_candidate_raw_evidence_registry(
            plan_path=raw_registry_fixture["plan_path"],
            run_paths=run_paths,
            workspace_root=raw_registry_fixture["workspace_root"],
        )


def test_candidate_raw_registry_rejects_substituted_base_registry_artifact(
    raw_registry_fixture: Mapping[str, Any],
) -> None:
    raw_registry_fixture["plan"]["artifacts"]["screening_base_pass_registry"][
        "path"
    ] = "substituted-base-pass-registry.json"

    with pytest.raises(ValidationError, match="PassRegistry bridge differs"):
        _build_registry(raw_registry_fixture)


def test_candidate_raw_registry_rejects_recorded_run_path_tamper(
    raw_registry_fixture: Mapping[str, Any],
) -> None:
    registry = _build_registry(raw_registry_fixture)
    source = raw_registry_fixture["run_paths"]["main.b1.candidate"]
    alias = raw_registry_fixture["workspace_root"] / "runs" / "same-bytes.json"
    alias.write_bytes(source.read_bytes())
    registry["runs"][0]["run_record"]["path"] = alias.relative_to(
        raw_registry_fixture["workspace_root"]
    ).as_posix()
    _rederive_registry_id(registry)
    registry_path = raw_registry_fixture["workspace_root"] / "raw-registry.json"
    atomic_write_json(registry_path, registry)

    with pytest.raises(ValidationError, match="run-path set differs"):
        candidate_module._load_and_reverify_candidate_raw_evidence_registry(
            plan=raw_registry_fixture["plan"],
            registry_path=registry_path,
            workspace_root=raw_registry_fixture["workspace_root"],
            expected_run_paths=raw_registry_fixture["run_paths"],
        )


@pytest.mark.parametrize(
    ("artifact_field", "verification_field"),
    (
        ("canonical_sha256", "run_canonical_sha256"),
        ("physical_sha256", "run_physical_sha256"),
    ),
)
def test_candidate_raw_registry_replay_rejects_internally_consistent_run_hash_tamper(
    raw_registry_fixture: Mapping[str, Any],
    artifact_field: str,
    verification_field: str,
) -> None:
    registry = _build_registry(raw_registry_fixture)
    row = registry["runs"][0]
    replacement = SHA_D
    row["run_record"][artifact_field] = replacement
    row["verification"][verification_field] = replacement
    _recommit_verification(row["verification"])
    _rederive_registry_id(registry)
    validate_document(registry)
    registry_path = raw_registry_fixture["workspace_root"] / "raw-registry.json"
    atomic_write_json(registry_path, registry)

    with pytest.raises(ValidationError, match="differs from replayed journals/raw files"):
        candidate_module._load_and_reverify_candidate_raw_evidence_registry(
            plan=raw_registry_fixture["plan"],
            registry_path=registry_path,
            workspace_root=raw_registry_fixture["workspace_root"],
        )


def test_candidate_raw_registry_rejects_raw_commitment_tamper(
    raw_registry_fixture: Mapping[str, Any],
) -> None:
    registry = _build_registry(raw_registry_fixture)
    registry["runs"][0]["verification"]["raw_evidence_sha256"] = SHA_D
    _rederive_registry_id(registry)

    with pytest.raises(ValidationError, match="raw evidence commitment differs"):
        validate_document(registry)


def test_candidate_raw_remark_callback_rejects_normalized_summary_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path = tmp_path / "run.json"
    run_path.write_text("{}\n", encoding="utf-8")
    remark_path = tmp_path / "remarks.jsonl"
    remark_path.write_text("{}\n", encoding="utf-8")
    run = {
        "schema_version": "run-record.v1",
        "run_id": "run-remark-mismatch",
        "configuration": {"enabled_candidate_ids": ["candidate.a"]},
        "provenance": {
            "pipeline_profile_id": "profile-candidate-a",
            "pipeline_profile_sha256": SHA_A,
        },
        "cases": [
            {
                "case_id": "case-remark",
                "remarks_event_count": 2,
                "candidate_remark_summary": {
                    "event_count": 2,
                    "summary_count": 0,
                    "paired_candidate_count": 0,
                    "applied_count": 0,
                    "rejected_count": 0,
                    "candidates": [],
                },
            }
        ],
    }
    catalog = {
        "schema_version": "candidate-catalog.v1",
        "candidates": [{"candidate_id": "candidate.a"}],
    }
    pass_registry = {"schema_version": "pass-registry.v2", "passes": []}
    monkeypatch.setattr(
        candidate_module,
        "_load_version",
        lambda *_args, **_kwargs: deepcopy(run),
    )
    monkeypatch.setattr(
        candidate_module,
        "validate_candidate_remark_jsonl",
        lambda *_args, **_kwargs: {
            "event_count": 1,
            "summary_count": 0,
            "paired_candidate_count": 0,
            "applied_count": 0,
            "rejected_count": 0,
            "by_candidate": {
                "candidate.a": {
                    "paired_candidate_count": 0,
                    "applied_count": 0,
                    "rejected_count": 0,
                }
            },
        },
    )

    def fake_verify_run_raw_evidence(
        path: Path,
        state_root: Path,
        *,
        remark_validator: Any,
    ) -> VerifiedRunRawEvidence:
        del path, state_root
        remark_validator(remark_path, run["cases"][0])
        raise AssertionError("summary mismatch must stop verification")

    monkeypatch.setattr(
        candidate_module,
        "verify_run_raw_evidence",
        fake_verify_run_raw_evidence,
    )

    with pytest.raises(ValidationError, match="raw remark summary differs"):
        candidate_module._verify_candidate_run_raw_evidence(
            run_path=run_path,
            state_root=tmp_path,
            catalog=catalog,
            pass_registry=pass_registry,
        )
