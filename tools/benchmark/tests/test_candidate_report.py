from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest

from tools.benchmark import cli, report
from tools.benchmark.errors import ConfigurationError, ValidationError
from tools.benchmark.schema import (
    _LOCKED_CANDIDATE_SCREENING_CONTRACT,
    validate_document,
)
from tools.benchmark.util import (
    atomic_write_json,
    atomic_write_text,
    sha256_file,
    sha256_json,
)


SHA = "0" * 64
NOW = "2026-08-11T00:00:00Z"


def _json_artifact(root: Path, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = root.joinpath(*key.split("/")[1:])
    atomic_write_json(path, payload)
    return {
        "artifact_key": key,
        "physical_sha256": sha256_file(path),
        "canonical_sha256": sha256_json(payload),
    }


def test_candidate_implementation_queue_uses_all_locked_tiebreaks() -> None:
    def candidate(
        implementation_id: str,
        speedup: float,
        *,
        risk: str,
        cost: str,
        qualified: bool = True,
    ) -> dict[str, Any]:
        return {
            "candidate_id": implementation_id.removeprefix("candidate-"),
            "implementation_candidate_id": implementation_id,
            "qualification_status": "qualified" if qualified else "rejected",
            "risk": risk,
            "implementation_cost": cost,
            "oracle_structures": [
                {
                    "eligible_for_candidate_screening": True,
                    "eligible_for_ranking": True,
                    "geometric_mean_speedup": speedup,
                }
            ],
        }

    queue = report._candidate_implementation_queue(
        [
            candidate("candidate-z", 1.30, risk="high", cost="high"),
            candidate("candidate-risk", 1.20, risk="medium", cost="low"),
            candidate("candidate-cost", 1.20, risk="low", cost="high"),
            candidate("candidate-b", 1.20, risk="low", cost="low"),
            candidate("candidate-a", 1.20, risk="low", cost="low"),
            candidate(
                "candidate-rejected",
                9.99,
                risk="low",
                cost="low",
                qualified=False,
            ),
        ]
    )
    assert [item["implementation_candidate_id"] for item in queue] == [
        "candidate-z",
        "candidate-a",
        "candidate-b",
        "candidate-cost",
        "candidate-risk",
    ]


def test_report_output_directory_rejects_symlink_traversal(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "report-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValidationError, match="symbolic link"):
        report._prepare_report_output_directory(link, label="candidate report output")
    with pytest.raises(ConfigurationError, match="symbolic link"):
        cli._workspace_output_path(
            tmp_path, link / "nested-report", label="candidate report output"
        )
    victim = target / "victim.json"
    victim.write_bytes(b"victim-must-not-change\n")
    output_link = tmp_path / "candidate-final.json"
    output_link.symlink_to(victim)
    with pytest.raises(ConfigurationError, match="symbolic link"):
        cli._workspace_immutable_output_path(
            tmp_path, output_link, label="candidate evidence output"
        )
    assert victim.read_bytes() == b"victim-must-not-change\n"
    immutable = tmp_path / "immutable.json"
    cli._publish_immutable_json(
        immutable, {"value": "first"}, label="candidate evidence output"
    )
    original_bytes = immutable.read_bytes()
    original_mtime_ns = immutable.stat().st_mtime_ns
    cli._publish_immutable_json(
        immutable, {"value": "first"}, label="candidate evidence output"
    )
    assert immutable.read_bytes() == original_bytes
    assert immutable.stat().st_mtime_ns == original_mtime_ns
    with pytest.raises(ConfigurationError, match="different bytes"):
        cli._publish_immutable_json(
            immutable, {"value": "second"}, label="candidate evidence output"
        )
    assert immutable.read_bytes() == original_bytes


def test_screening_report_binds_base_registry_without_exposing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.benchmark import candidates as candidate_module
    from tools.benchmark.tests.test_candidates import (
        _patch_screening_inputs,
        _screening_documents,
    )

    evidence, spec, capture = _screening_documents()
    _patch_screening_inputs(monkeypatch, evidence, spec, capture)
    screening = candidate_module.build_candidate_screening(
        candidate_evidence_path=Path("evidence.json"),
        screening_spec_path=Path("spec.json"),
        pass_registry_path=Path("registry.json"),
        oracle_capture_path=Path("capture.json"),
        workspace_root=Path("."),
        screening_id="screening-report-registry",
    )

    first = report.build_candidate_screening_report(
        screening=screening,
        output_directory=tmp_path / "first",
    )["CANDIDATE_SCREENING_REPORT.zh-CN.md"]
    second = report.build_candidate_screening_report(
        screening=screening,
        output_directory=tmp_path / "second",
    )["CANDIDATE_SCREENING_REPORT.zh-CN.md"]
    assert sha256_file(first) == sha256_file(second)
    markdown = first.read_text(encoding="utf-8")
    assert (
        f"`pass_registry_sha256`）：`{screening['pass_registry_sha256']}`"
        in markdown
    )
    assert (
        "筛选基线 PassRegistry artifact canonical / physical SHA-256："
        f"`{screening['base_pass_registry']['canonical_sha256']}` / "
        f"`{screening['base_pass_registry']['physical_sha256']}`"
        in markdown
    )
    assert screening["base_pass_registry"]["path"] not in markdown
    assert "n/ax" not in markdown
    assert "n/a" in markdown

    tampered = deepcopy(screening)
    tampered["base_pass_registry"]["canonical_sha256"] = "e" * 64
    with pytest.raises(ValidationError, match="base PassRegistry"):
        report.build_candidate_screening_report(
            screening=tampered,
            output_directory=tmp_path / "tampered",
        )


def _physical_artifact(root: Path, key: str, payload: str) -> dict[str, Any]:
    path = root.joinpath(*key.split("/")[1:])
    atomic_write_text(path, payload)
    return {"artifact_key": key, "physical_sha256": sha256_file(path)}


def _r7_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    campaign = tmp_path / "campaign"
    runs = tmp_path / "runs"
    workspace.mkdir()
    campaign.mkdir()
    runs.mkdir()
    campaign_plan = _json_artifact(
        campaign, "campaign/initial.plan.json", {"kind": "plan"}
    )
    protocols = [
        _json_artifact(
            workspace,
            f"repository/docs/protocol-{index}.json",
            {"kind": "protocol", "index": index},
        )
        for index in range(2)
    ]
    controllers = [
        _physical_artifact(
            campaign, f"campaign/controller-{index}.sh", f"controller-{index}\n"
        )
        for index in range(2)
    ]
    registry = _physical_artifact(
        campaign, "campaign/run-evidence.tsv", "registered\n"
    )
    registry.update(
        line_count=21,
        completed_count=20,
        failed_count=1,
        partial_run_registered=False,
    )
    statuses = [
        {
            **_json_artifact(
                campaign,
                f"campaign/status/{index:03d}.json",
                {"sequence": index},
            ),
            "sequence": index,
        }
        for index in range(28)
    ]
    registered = [
        {
            **_json_artifact(
                runs,
                f"runs/terminal-{index}/run.json",
                {"run": index},
            ),
            "state": "failed" if index == 6 else "completed",
        }
        for index in range(21)
    ]
    partial = {
        **_json_artifact(
            runs, "runs/partial/run.json", {"run": "partial"}
        ),
        "summary": {
            "total_cases": 20,
            "passed_cases": 16,
            "failed_cases": 0,
            "pending_cases": 4,
            "censored_cases": 0,
        },
        "resume_allowed": False,
    }
    frozen = {
        "schema_version": "diagnostic-freeze.v1",
        "freeze_id": "diagnostic-freeze:r7",
        "classification": "diagnostic_aborted_direction_mismatch",
        "eligibility": {
            "diagnostic_only": True,
            "ranking_eligible": False,
            "promotion_eligible": False,
            "auto_resume_allowed": False,
            "manual_resume_allowed": False,
        },
        "bindings": {
            "campaign_plan": campaign_plan,
            "measurement_protocols": protocols,
            "controllers": controllers,
            "run_evidence_registry": registry,
            "status_ledger": {"entries": statuses},
        },
        "registered_terminal_runs": registered,
        "unregistered_partial_run": partial,
        "provenance_limitations": [
            {
                "code": "source_to_wsl_ignored_tree_full_hash_not_completed",
                "effect": "whole tree equality is not proven",
            }
        ],
    }
    freeze_path = workspace / "r7.freeze.json"
    atomic_write_json(freeze_path, frozen)
    return freeze_path, workspace, campaign, runs


def _outcome(
    candidate_id: str,
    role: str,
    speedup: float,
    *,
    count: int,
) -> dict[str, Any]:
    return {
        "study_id": f"study-{role}",
        "run_id": f"run-{role}-{candidate_id}",
        "run_sha256": sha256_json({"run": role, "candidate": candidate_id}),
        "configuration_sha256": sha256_json(
            {"configuration": role, "candidate": candidate_id}
        ),
        "suite_id": f"suite-{role}",
        "manifest_sha256": sha256_json({"manifest": role}),
        "expected_case_count": count,
        "eligible_for_ranking": True,
        "ineligibility_reason": None,
        "comparable_cases": count,
        "comparable_source_groups": count,
        "correctness_failures": 0,
        "censored_cases": 0,
        "excluded_cases": 0,
        "case_geometric_mean_speedup": speedup,
        "source_group_geometric_mean_speedup": speedup,
        "confidence_interval_95": {"low": speedup * 0.99, "high": speedup * 1.01},
        "static_text_bytes_full": 1000.0,
        "static_text_bytes_full_plus_candidate": 1010.0,
        "static_text_ratio": 1000.0 / 1010.0,
    }


def _b1(candidate_id: str) -> dict[str, Any]:
    return {
        "run_id": f"run-B1-{candidate_id}",
        "run_sha256": sha256_json({"run": "B1", "candidate": candidate_id}),
        "configuration_sha256": sha256_json({"configuration": candidate_id}),
        "suite_id": "suite-B1",
        "manifest_sha256": sha256_json({"manifest": "B1"}),
        "case_count": 140,
        "evidence_level": "qemu_correctness",
        "state": "completed",
        "passed_cases": 140,
        "failed_cases": 0,
        "pending_cases": 0,
        "censored_cases": 0,
        "all_correct": True,
        "failure_reason": None,
    }


def _run(
    run_id: str,
    role: str,
    *,
    profile_id: str,
    enabled: list[str],
    state: str = "completed",
    total: int = 60,
) -> dict[str, Any]:
    passed = total if state == "completed" else 0
    pending = 0 if state != "interrupted" else total
    failed = total if state == "failed" else 0
    return {
        "schema_version": "run-record.v1",
        "run_id": run_id,
        "state": state,
        "suite_id": f"suite-{role}",
        "manifest_sha256": sha256_json({"manifest": role}),
        "configuration_sha256": sha256_json({"run": run_id}),
        "configuration": {
            "evidence_level": (
                "qemu_correctness" if role == "B1" else "qemu_proxy"
            ),
            "enabled_candidate_ids": enabled,
            "candidate_registry_sha256": "1" * 64,
            "candidate_pass_registry_sha256": "6" * 64,
        },
        "provenance": {
            "pipeline_profile_id": profile_id,
            "compiler_artifact_sha256": "3" * 64,
            "measurement_protocol_sha256": "4" * 64,
        },
        "summary": {
            "total_cases": total,
            "passed_cases": passed,
            "failed_cases": failed,
            "pending_cases": pending,
            "censored_cases": 0,
        },
        "cases": [
            {"data_role": role, "status": "pending" if pending else "passed"}
        ],
    }


def _screening() -> dict[str, Any]:
    rows = []
    qualified = {"boom_ilp", "closed_form", "dp_storage", "finite_state"}
    locked = {
        item[0]: item for item in _LOCKED_CANDIDATE_SCREENING_CONTRACT
    }
    for family_id, _, _ in report._CANDIDATE_SCREENING_FAMILIES:
        eligible_family = locked[family_id][3] == "eligible"
        implementation_id = (
            f"candidate-{family_id}" if eligible_family else None
        )
        status = (
            "qualified"
            if family_id in qualified
            else (
                "rejected"
                if locked[family_id][3] == "rejected"
                else "blocked"
            )
        )
        eligible_refs = [
            {"oracle_family_id": source_family, "structure_id": structure_id}
            for source_family, structure_id in locked[family_id][5]
        ]
        rows.append(
            {
                "candidate_id": family_id,
                "implementation_candidate_id": implementation_id,
                "qualification_status": status,
                "eligible_oracle_structure_refs": eligible_refs,
                "qualifying_oracle_structure_refs": (
                    deepcopy(eligible_refs) if family_id in qualified else []
                ),
                "oracle_structures": [
                    {
                        "oracle_family_id": ref["oracle_family_id"],
                        "structure_id": ref["structure_id"],
                        "eligible_for_candidate_screening": True,
                        "eligible_for_ranking": family_id in qualified,
                        "geometric_mean_speedup": (
                            1.2 if family_id in qualified else None
                        ),
                    }
                    for ref in eligible_refs
                ],
                "overlaps_existing_pass_ids": [],
                "legality_proof_path": "clear" if eligible_family else "unclear",
                "legality_obligation_ids": (
                    [f"{family_id}-proof"] if eligible_family else []
                ),
                "implementation_cost": "medium",
                "risk": "high" if family_id == "dp_storage" else "medium",
                "rejection_reasons": (
                    []
                    if family_id in qualified
                    else [
                        "blocked_locked_bitset_capability_gap"
                        if family_id == "bitset"
                        else "mixed_family_no_unified_transform"
                        if locked[family_id][3] == "rejected"
                        else "no_complete_oracle_structure"
                    ]
                ),
            }
        )
    return {
        "schema_version": "candidate-screening.v1",
        "screening_id": "screening-test",
        "pass_registry_sha256": "2" * 64,
        "base_pass_registry": {
            "path": "registries/screening-base.json",
            "canonical_sha256": "2" * 64,
            "physical_sha256": "7" * 64,
        },
        "candidates": rows,
    }


def _final(screening: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    speedups = {
        "candidate-boom_ilp": 0.99,
        "candidate-closed_form": 0.98,
        "candidate-dp_storage": 0.97,
        "candidate-finite_state": 0.96,
    }
    for screened in screening["candidates"]:
        implementation_id = screened["implementation_candidate_id"]
        if screened["qualification_status"] != "qualified":
            b1 = None
            b2 = None
            suites = {role: None for role in ("B3", "B4", "B5", "B6")}
        else:
            b1 = _b1(implementation_id)
            b2 = _outcome(implementation_id, "B2", 1.01, count=20)
            suites = {
                "B3": _outcome(
                    implementation_id,
                    "B3",
                    speedups[implementation_id],
                    count=60,
                ),
                "B4": None,
                "B5": None,
                "B6": None,
            }
        candidates.append(
            {
                "candidate_id": screened["candidate_id"],
                "implementation_candidate_id": implementation_id,
                "b1_correctness": b1,
                "b2_tuning": b2,
                "suite_outcomes": suites,
            }
        )
    return {
        "schema_version": "candidate-final.v1",
        "final_id": "candidate-final-test",
        "generated_at": NOW,
        "screening_sha256": sha256_json(screening),
        "candidate_registry_sha256": "1" * 64,
        "executable_pass_registry_sha256": "6" * 64,
        "expected_combined_case_count": 267,
        "studies": {
            "B3": {
                "study_id": "study-B3",
                "suite_id": "suite-B3",
                "manifest_sha256": sha256_json({"manifest": "B3"}),
                "baseline_run_id": "run-B3-full",
                "baseline_run_sha256": SHA,
                "baseline_configuration_sha256": SHA,
            }
        },
        "candidates": candidates,
        "ranking": [],
        "winner_candidate_id": None,
        "winner_reason": "no_winning_candidate",
    }


def _diagnostic(final: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    singles = []
    speedups: dict[str, float] = {}
    ranked = sorted(
        (
            item
            for item in final["candidates"]
            if item["suite_outcomes"]["B3"] is not None
        ),
        key=lambda item: (
            -item["suite_outcomes"]["B3"][
                "case_geometric_mean_speedup"
            ],
            item["implementation_candidate_id"],
        ),
    )[:3]
    for item in ranked:
        outcome = item["suite_outcomes"]["B3"]
        if outcome is None:
            continue
        singles.append(
            {
                "candidate_id": item["implementation_candidate_id"],
                "run_id": outcome["run_id"],
                "run_sha256": outcome["run_sha256"],
                "configuration_sha256": outcome["configuration_sha256"],
            }
        )
        speedups[item["implementation_candidate_id"]] = outcome[
            "case_geometric_mean_speedup"
        ]
    ids = [item["candidate_id"] for item in singles]
    interactions = []
    for left_index, left in enumerate(ids):
        for right in ids[left_index + 1 :]:
            expected = speedups[left] * speedups[right]
            pair_speedup = expected * 1.01
            failed_pair = not interactions
            stable_pair = "+".join(sorted((left, right)))
            interactions.append(
                {
                    "candidate_ids": [left, right],
                    "run_id": f"run-pair-{stable_pair}",
                    "run_sha256": sha256_json({"pair": stable_pair}),
                    "configuration_sha256": sha256_json(
                        {"pair_configuration": stable_pair}
                    ),
                    "comparable_cases": 0 if failed_pair else 60,
                    "correctness_failures": 0,
                    "censored_cases": 60 if failed_pair else 0,
                    "excluded_cases": 0,
                    "eligible_for_ranking": not failed_pair,
                    "ineligibility_reason": (
                        "right_censored" if failed_pair else None
                    ),
                    "pair_case_geometric_mean_speedup": (
                        None if failed_pair else pair_speedup
                    ),
                    "expected_multiplicative_speedup": (
                        None if failed_pair else expected
                    ),
                    "delta_ln_geometric_mean": (
                        None
                        if failed_pair
                        else math.log(pair_speedup) - math.log(expected)
                    ),
                }
            )
    return {
        "schema_version": "candidate-study.v1",
        "study_id": "diagnostic-B3",
        "data_role": "B3",
        "candidate_registry_sha256": final["candidate_registry_sha256"],
        "suite_id": full["suite_id"],
        "manifest_sha256": full["manifest_sha256"],
        "baseline": {
            "run_id": full["run_id"],
            "run_sha256": sha256_json(full),
            "configuration_sha256": full["configuration_sha256"],
        },
        "candidates": singles,
        "interactions": interactions,
    }


def test_candidate_report_is_deterministic_and_keeps_failed_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    screening = _screening()
    final = _final(screening)
    b1_full = _run(
        "run-B1-full", "B1", profile_id="candidate-empty", enabled=[], total=140
    )
    final["b1_full_correctness"] = {
        "run_id": b1_full["run_id"],
        "run_sha256": sha256_json(b1_full),
        "configuration_sha256": b1_full["configuration_sha256"],
        "suite_id": b1_full["suite_id"],
        "manifest_sha256": b1_full["manifest_sha256"],
        "case_count": 140,
        "evidence_level": "qemu_correctness",
        "state": "completed",
        "passed_cases": 140,
        "failed_cases": 0,
        "pending_cases": 0,
        "censored_cases": 0,
        "all_correct": True,
        "failure_reason": None,
    }
    full = _run(
        "run-B3-full", "B3", profile_id="candidate-empty", enabled=[]
    )
    final["studies"]["B3"]["baseline_run_sha256"] = sha256_json(full)
    final["studies"]["B3"]["baseline_configuration_sha256"] = full[
        "configuration_sha256"
    ]
    diagnostic = _diagnostic(final, full)
    gcc = _run(
        "run-B3-gcc", "B3", profile_id="gcc-13.3-o2", enabled=[], state="failed"
    )
    clang = _run(
        "run-B3-clang", "B3", profile_id="clang-18-o3", enabled=[]
    )
    final["campaign"] = {
        "run_records": [
            {
                "task_id": "run.B3.gcc",
                "run_id": gcc["run_id"],
                "run_sha256": sha256_json(gcc),
                "state": gcc["state"],
            },
            {
                "task_id": "run.B3.clang",
                "run_id": clang["run_id"],
                "run_sha256": sha256_json(clang),
                "state": clang["state"],
            },
        ]
    }
    final["freeze"] = {
        "freeze_id": "candidate-freeze:test",
        "freeze_sha256": "8" * 64,
        "campaign_id": "candidate-campaign:test",
        "repo_commit": "a" * 40,
        "repo_tree": "b" * 40,
        "screening_base_pass_registry": {
            "path": "registries/screening-base.json",
            "canonical_sha256": "2" * 64,
            "physical_sha256": "7" * 64,
        },
        "executable_pass_registry": {
            "path": "registries/executable.json",
            "canonical_sha256": "6" * 64,
            "physical_sha256": "f" * 64,
        },
        "compiler_artifact": {"physical_sha256": "3" * 64},
        "base_pipeline_profile": {
            "path": "profiles/candidate-empty.json",
            "canonical_sha256": "9" * 64,
            "physical_sha256": "a" * 64,
        },
        "standard_measurement_protocol": {
            "path": "protocols/standard.json",
            "canonical_sha256": "4" * 64,
            "physical_sha256": "b" * 64,
        },
        "hotblock_measurement_protocol": {
            "path": "protocols/hotblock.json",
            "canonical_sha256": "4" * 64,
            "physical_sha256": "c" * 64,
        },
        "reference_toolchain": {
            "snapshot": {
                "path": "toolchains/reference.json",
                "canonical_sha256": "d" * 64,
                "physical_sha256": "e" * 64,
            },
            "common_tool_versions": {
                "qemu-system-riscv64": "9.2.4",
                "bare-metal-linker": "GNU ld 2.44",
                "python": "3.12.11",
                "glib": "2.84.2",
            },
            "accela_jdk_version": "OpenJDK 21.0.8",
            "baselines": [
                {
                    "compiler_baseline": "gcc_13_3_o2",
                    "profile_id": "gcc-13.3-o2",
                    "profile_sha256": "1" * 64,
                    "tool": "riscv-gcc",
                    "version": "13.3.0",
                    "optimization": "-O2",
                    "compiler_command_sha256": "2" * 64,
                    "compiler_argv_sha256": "3" * 64,
                },
                {
                    "compiler_baseline": "clang_18_o3",
                    "profile_id": "clang-18-o3",
                    "profile_sha256": "4" * 64,
                    "tool": "clang",
                    "version": "18.1.3",
                    "optimization": "-O3",
                    "compiler_command_sha256": "5" * 64,
                    "compiler_argv_sha256": "6" * 64,
                },
            ],
        },
    }
    top3 = [
        "candidate-boom_ilp",
        "candidate-closed_form",
        "candidate-dp_storage",
    ]
    hotblock = {
        "cache-full": _run(
            "cache-full", "B3", profile_id="candidate-empty", enabled=[]
        ),
        **{
            f"cache-{candidate_id}": _run(
                f"cache-{candidate_id}",
                "B3",
                profile_id=f"single-{candidate_id}",
                enabled=[candidate_id],
                state=("interrupted" if index == 1 else "completed"),
            )
            for index, candidate_id in enumerate(top3)
        },
    }
    documents: dict[Path, dict[str, Any]] = {}

    def bind(name: str, document: dict[str, Any]) -> Path:
        path = tmp_path / "inputs" / f"{name}.json"
        atomic_write_json(path, {"logical_input": name})
        documents[path] = document
        return path

    final_path = bind("final", final)
    screening_path = bind("screening", screening)
    b1_full_path = bind("b1-full", b1_full)
    full_path = bind("b3-full", full)
    diagnostic_path = bind("diagnostic", diagnostic)
    comparison_paths = {"gcc-13.3-o2": bind("gcc", gcc), "clang-18-o3": bind("clang", clang)}
    hotblock_paths = {
        label: bind(label, run) for label, run in hotblock.items()
    }
    final["freeze"]["screening"] = {
        "canonical_sha256": sha256_json(screening),
        "physical_sha256": sha256_file(screening_path),
    }

    def campaign_run(
        task_id: str, path: Path, run: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "run_id": run["run_id"],
            "run_sha256": sha256_json(run),
            "run_physical_sha256": sha256_file(path),
            "state": run["state"],
        }

    final["campaign"] = {
        "run_records": [
            campaign_run("run.B1.full", b1_full_path, b1_full),
            campaign_run("run.B3.full", full_path, full),
            campaign_run(
                "run.B3.gcc", comparison_paths["gcc-13.3-o2"], gcc
            ),
            campaign_run(
                "run.B3.clang", comparison_paths["clang-18-o3"], clang
            ),
            *[
                campaign_run(
                    (
                        "diagnostic.cache.full"
                        if not run["configuration"]["enabled_candidate_ids"]
                        else "diagnostic.cache."
                        + run["configuration"]["enabled_candidate_ids"][0]
                    ),
                    hotblock_paths[label],
                    run,
                )
                for label, run in hotblock.items()
            ],
        ]
    }
    pair_tasks = []
    for interaction in diagnostic["interactions"]:
        candidate_ids = sorted(interaction["candidate_ids"])
        pair_tasks.append(
            {
                "task_id": f"diagnostic.pair.{'+'.join(candidate_ids)}",
                "kind": "pair",
                "candidate_ids": candidate_ids,
                "run_id": interaction["run_id"],
                "evidence_sha256": interaction["run_sha256"],
                "evidence_physical_sha256": "5" * 64,
                "configuration_sha256": interaction[
                    "configuration_sha256"
                ],
                "status": (
                    "completed"
                    if interaction["eligible_for_ranking"]
                    else "failed"
                ),
                "failure_reason": (
                    None
                    if interaction["eligible_for_ranking"]
                    else "timeout"
                ),
            }
        )
    cache_tasks = []
    for label, run in hotblock.items():
        enabled = run["configuration"]["enabled_candidate_ids"]
        cache_tasks.append(
            {
                "task_id": (
                    "diagnostic.cache.full"
                    if not enabled
                    else f"diagnostic.cache.{enabled[0]}"
                ),
                "kind": "cache_hotblock",
                "candidate_ids": enabled,
                "run_id": run["run_id"],
                "evidence_sha256": sha256_json(run),
                "evidence_physical_sha256": sha256_file(
                    hotblock_paths[label]
                ),
                "configuration_sha256": run["configuration_sha256"],
                "status": run["state"],
                "failure_reason": (
                    None if run["state"] == "completed" else "tool_failure"
                ),
            }
        )
    final["diagnostics"] = {
        "top3_candidate_ids": top3,
        "tasks": [*pair_tasks, *cache_tasks],
        "study_status": "completed",
        "study_ineligibility_reason": None,
        "study": {
            "study_id": diagnostic["study_id"],
            "canonical_sha256": sha256_json(diagnostic),
            "physical_sha256": sha256_file(diagnostic_path),
        },
    }
    r7_path, workspace, campaign, runs = _r7_fixture(tmp_path)
    campaign_plan_path = workspace / "candidate-campaign-plan.json"
    completed_status_path = workspace / "candidate-campaign-completed.json"
    completed_ledger_paths = [
        workspace / "candidate-status-000.json",
        workspace / "candidate-status-001.json",
    ]
    atomic_write_json(campaign_plan_path, {"logical_input": "campaign-plan"})
    atomic_write_json(completed_status_path, {"logical_input": "completed-status"})
    for index, path in enumerate(completed_ledger_paths):
        atomic_write_json(path, {"logical_input": f"status-{index}"})

    def fake_load(path: Path, version: str) -> dict[str, Any]:
        assert path in documents
        assert documents[path]["schema_version"] == version
        return deepcopy(documents[path])

    monkeypatch.setattr(report, "_load_version", fake_load)
    monkeypatch.setattr(report, "_require_formal_measurement", lambda *args, **kwargs: None)
    campaign_completion = {
        "campaign_id": "candidate-campaign:test",
        "plan_sha256": sha256_json({"logical_input": "campaign-plan"}),
        "candidate_final_sha256": sha256_json(final),
        "candidate_final_physical_sha256": sha256_file(final_path),
        "completed_status_sha256": "6" * 64,
        "completed_status_physical_sha256": sha256_file(
            completed_status_path
        ),
        "status_ledger_entry_count": 2,
        "status_ledger_head_sha256": "6" * 64,
        "status_ledger_sha256": "7" * 64,
        "raw_evidence_registry_sha256": "8" * 64,
        "raw_evidence_registry_physical_sha256": "9" * 64,
    }

    def fake_final_completion(**arguments: Any) -> dict[str, Any]:
        assert arguments == {
            "campaign_plan_path": campaign_plan_path,
            "candidate_final_path": final_path,
            "completed_status_path": completed_status_path,
            "status_ledger_paths": completed_ledger_paths,
            "workspace_root": workspace,
        }
        return deepcopy(campaign_completion)

    monkeypatch.setattr(
        report, "validate_candidate_final_completion", fake_final_completion
    )
    comparison = SimpleNamespace(
        pairs=(object(),),
        correctness_failures=0,
        censored_cases=0,
        excluded_cases=0,
        geometric_mean_speedup=1.2,
    )
    monkeypatch.setattr(report, "compare_runs", lambda *args, **kwargs: comparison)
    monkeypatch.setattr(
        report, "bootstrap_geometric_mean_ci", lambda *args, **kwargs: (1.1, 1.3)
    )

    def fake_hotblock(runs_by_label: dict[str, dict[str, Any]], **_: Any) -> dict[str, Any]:
        run_rows = []
        samples = []
        for label, run in sorted(runs_by_label.items()):
            state = run["state"]
            run_rows.append(
                {
                    "label": label,
                    "run_id": run["run_id"],
                    "state": state,
                    "failure_classification": None if state == "completed" else f"run_{state}",
                    "total_cases": 60,
                    "passed_cases": 60 if state == "completed" else 0,
                    "failed_cases": 0,
                    "censored_cases": 0,
                }
            )
            if state == "completed":
                samples.append(
                    {
                        "run_id": run["run_id"],
                        "l1d_misses_per_1000_dynamic_loads": 2.0,
                        "hotblock_dynamic_instruction_share": 0.25,
                    }
                )
        return {"runs": run_rows, "samples": samples}

    monkeypatch.setattr(report, "_hotblock_diagnostics", fake_hotblock)

    kwargs = {
        "candidate_final_path": final_path,
        "campaign_plan_path": campaign_plan_path,
        "completed_campaign_status_path": completed_status_path,
        "completed_status_ledger_paths": completed_ledger_paths,
        "screening_path": screening_path,
        "b1_full_run_path": b1_full_path,
        "full_run_path": full_path,
        "diagnostic_study_path": diagnostic_path,
        "comparison_paths": comparison_paths,
        "hotblock_run_paths": hotblock_paths,
        "r7_freeze_path": r7_path,
        "workspace_root": workspace,
        "r7_campaign_root": campaign,
        "r7_runs_root": runs,
    }
    for terminal_error in (
        "candidate final is not registered by terminal status",
        "candidate terminal status ledger hash/time chain differs",
    ):
        def reject_completion(**_: Any) -> dict[str, Any]:
            raise ValidationError(terminal_error)

        monkeypatch.setattr(
            report, "validate_candidate_final_completion", reject_completion
        )
        with pytest.raises(ValidationError, match=terminal_error):
            report.build_candidate_report(
                output_directory=tmp_path / "invalid-terminal-closure",
                **kwargs,
            )
    monkeypatch.setattr(
        report, "validate_candidate_final_completion", fake_final_completion
    )
    with pytest.raises(
        ValidationError,
        match="winner identity and winner B3 run must be supplied together",
    ):
        report.build_candidate_report(
            output_directory=tmp_path / "winner-without-identity",
            winner_run_path=full_path,
            **kwargs,
        )
    documents[final_path]["winner_candidate_id"] = top3[0]
    with pytest.raises(
        ValidationError,
        match="winner identity and winner B3 run must be supplied together",
    ):
        report.build_candidate_report(
            output_directory=tmp_path / "winner-without-run",
            **kwargs,
        )
    documents[final_path]["winner_candidate_id"] = None
    first = tmp_path / "first"
    second = tmp_path / "second"
    summary = report.build_candidate_report(output_directory=first, **kwargs)
    repeated = report.build_candidate_report(output_directory=second, **kwargs)
    assert summary == repeated == validate_document(summary)
    normalized_strings: list[str] = []

    def collect_strings(value: Any) -> None:
        if isinstance(value, str):
            normalized_strings.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect_strings(key)
                collect_strings(item)
        elif isinstance(value, list):
            for item in value:
                collect_strings(item)

    collect_strings(summary)
    assert not any(
        value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)
        for value in normalized_strings
    )
    assert summary["conclusion"] == {
        "winner_candidate_id": None,
        "winner_reason": "no_winning_candidate",
        "claim": "no_winner",
    }
    gcc_row = next(
        row for row in summary["toolchain_context"] if row["label"] == "gcc-13.3-o2"
    )
    assert gcc_row["failure_classification"] == "run_failed"
    assert gcc_row["reference_over_full_geometric_mean"] is None
    clang_row = next(
        row for row in summary["toolchain_context"] if row["label"] == "clang-18-o3"
    )
    assert clang_row["reference_over_full_geometric_mean"] == pytest.approx(1.2)
    interrupted = next(
        row for row in summary["hotblock_diagnostics"] if row["state"] == "interrupted"
    )
    assert interrupted["mean_l1d_misses_per_1000_dynamic_loads"] is None
    failed_pair = next(
        row
        for row in summary["interactions"]
        if row["failure_classification"] == "right_censored"
    )
    assert failed_pair["pair_case_geometric_mean_speedup"] is None
    assert failed_pair["delta_ln_geometric_mean"] is None
    assert summary["r7_diagnostic_appendix"]["enumerated_bindings_rehashed"] == 56
    assert summary["frozen_context"]["repository_commit"] == "a" * 40
    assert [
        row["profile_id"]
        for row in summary["frozen_context"]["reference_toolchain"]["baselines"]
    ] == ["gcc-13.3-o2", "clang-18-o3"]
    assert summary["ranking"] == final["ranking"]
    assert summary["bindings"]["pass_registries"] == {
        "screening_base": {
            "declared_sha256": "2" * 64,
            "artifact": {
                "canonical_sha256": "2" * 64,
                "physical_sha256": "7" * 64,
            },
        },
        "executable": {
            "declared_sha256": "6" * 64,
            "artifact": {
                "canonical_sha256": "6" * 64,
                "physical_sha256": "f" * 64,
            },
        },
    }
    invalid_proof = deepcopy(summary)
    invalid_proof["screening"][0]["legality_proof_path"] = "invented"
    with pytest.raises(ValidationError):
        validate_document(invalid_proof)
    substituted_registry = deepcopy(summary)
    substituted_registry["bindings"]["pass_registries"]["screening_base"][
        "declared_sha256"
    ] = "f" * 64
    with pytest.raises(ValidationError, match="PassRegistry binding"):
        validate_document(substituted_registry)
    conflated_registries = deepcopy(summary)
    conflated_registries["bindings"]["pass_registries"]["executable"] = deepcopy(
        conflated_registries["bindings"]["pass_registries"]["screening_base"]
    )
    with pytest.raises(ValidationError, match="PassRegistry binding"):
        validate_document(conflated_registries)
    substituted_terminal_closure = deepcopy(summary)
    substituted_terminal_closure["bindings"]["campaign_completion"][
        "completed_status_sha256"
    ] = "f" * 64
    with pytest.raises(ValidationError, match="terminal campaign closure"):
        validate_document(substituted_terminal_closure)
    substituted_frozen_campaign = deepcopy(summary)
    substituted_frozen_campaign["frozen_context"]["campaign_id"] = (
        "candidate-campaign:other"
    )
    with pytest.raises(ValidationError, match="frozen campaign"):
        validate_document(substituted_frozen_campaign)
    reordered_toolchains = deepcopy(summary)
    reordered_toolchains["frozen_context"]["reference_toolchain"][
        "baselines"
    ].reverse()
    with pytest.raises(ValidationError, match="frozen GCC/Clang"):
        validate_document(reordered_toolchains)
    fabricated_winner_binding = deepcopy(summary)
    fabricated_winner_binding["bindings"]["winner_run"] = deepcopy(
        summary["bindings"]["b3_full_run"]
    )
    with pytest.raises(ValidationError, match="winner identity and winner run"):
        validate_document(fabricated_winner_binding)
    fabricated_failure_metric = deepcopy(summary)
    next(
        row
        for row in fabricated_failure_metric["toolchain_context"]
        if row["label"] == "gcc-13.3-o2"
    )["reference_over_full_geometric_mean"] = 1.0
    with pytest.raises(ValidationError, match="failed toolchain diagnostic"):
        validate_document(fabricated_failure_metric)
    fabricated_hotblock_metric = deepcopy(summary)
    next(
        row
        for row in fabricated_hotblock_metric["hotblock_diagnostics"]
        if row["state"] == "interrupted"
    )["mean_l1d_misses_per_1000_dynamic_loads"] = 1.0
    with pytest.raises(ValidationError, match="failed hotblock diagnostic"):
        validate_document(fabricated_hotblock_metric)
    original_executable_declared = final["executable_pass_registry_sha256"]
    original_executable_artifact = deepcopy(
        final["freeze"]["executable_pass_registry"]
    )
    final["executable_pass_registry_sha256"] = screening[
        "pass_registry_sha256"
    ]
    final["freeze"]["executable_pass_registry"] = deepcopy(
        final["freeze"]["screening_base_pass_registry"]
    )
    with pytest.raises(
        ValidationError,
        match="screening/executable PassRegistry binding differs",
    ):
        report.build_candidate_report(
            output_directory=tmp_path / "conflated-pass-registries", **kwargs
        )
    final["executable_pass_registry_sha256"] = original_executable_declared
    final["freeze"]["executable_pass_registry"] = original_executable_artifact
    first_interaction = documents[diagnostic_path]["interactions"][0]
    original_pair_sha256 = first_interaction["run_sha256"]
    original_study_sha256 = final["diagnostics"]["study"][
        "canonical_sha256"
    ]
    first_interaction["run_sha256"] = "f" * 64
    final["diagnostics"]["study"]["canonical_sha256"] = sha256_json(
        documents[diagnostic_path]
    )
    with pytest.raises(ValidationError, match="Top3 pair run identity differs"):
        report.build_candidate_report(
            output_directory=tmp_path / "substituted-pair", **kwargs
        )
    first_interaction["run_sha256"] = original_pair_sha256
    final["diagnostics"]["study"][
        "canonical_sha256"
    ] = original_study_sha256
    original_delta = first_interaction["delta_ln_geometric_mean"]
    first_interaction["delta_ln_geometric_mean"] = 0.5
    with pytest.raises(ValidationError, match="diagnostic study dual-hash differs"):
        report.build_candidate_report(
            output_directory=tmp_path / "substituted-pair-metric", **kwargs
        )
    first_interaction["delta_ln_geometric_mean"] = original_delta
    full_hotblock = documents[hotblock_paths["cache-full"]]
    original_hotblock_state = full_hotblock["state"]
    full_hotblock["state"] = "failed"
    with pytest.raises(ValidationError, match="final campaign evidence"):
        report.build_candidate_report(
            output_directory=tmp_path / "substituted-hotblock", **kwargs
        )
    full_hotblock["state"] = original_hotblock_state
    documents[final_path]["b1_full_correctness"]["run_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="B1 FULL baseline identity differs"):
        report.build_candidate_report(
            output_directory=tmp_path / "substituted-b1", **kwargs
        )

    expected_files = {
        "FINAL_CANDIDATE_REPORT.zh-CN.md",
        "candidate-report.v1.json",
        *report._CANDIDATE_REPORT_CSV_FIELDS,
        *report._CANDIDATE_REPORT_SVG_FILES,
    }
    assert {path.name for path in first.iterdir()} == expected_files
    assert {path.name for path in second.iterdir()} == expected_files
    first_hashes = {
        path.name: sha256_file(path) for path in sorted(first.iterdir())
    }
    second_hashes = {
        path.name: sha256_file(path) for path in sorted(second.iterdir())
    }
    assert first_hashes == second_hashes
    local_root = str(tmp_path).encode()
    assert all(local_root not in path.read_bytes() for path in first.iterdir())
    assert all(
        b"profiles/candidate-empty.json" not in path.read_bytes()
        and b"protocols/standard.json" not in path.read_bytes()
        and b"toolchains/reference.json" not in path.read_bytes()
        for path in first.iterdir()
    )
    assert len(list(first.glob("*.svg"))) == 7
    for svg_path in sorted(first.glob("*.svg")):
        assert ElementTree.parse(svg_path).getroot().tag.endswith("svg")
    assert "100×ΔlnGM" in (first / "candidate-interaction-heatmap.svg").read_text(
        encoding="utf-8"
    )
    pareto_svg = (first / "candidate-pareto.svg").read_text(encoding="utf-8")
    assert "静态 text bytes" in pareto_svg
    assert "颜色＋文字" in pareto_svg
    assert "risk=" in pareto_svg
    cache_svg = (first / "candidate-cache-hotblock.svg").read_text(
        encoding="utf-8"
    )
    assert "L1D misses / 1000 dynamic loads" in cache_svg
    assert "最热基本块动态指令占比（%）" in cache_svg
    markdown = (first / "FINAL_CANDIDATE_REPORT.zh-CN.md").read_text(encoding="utf-8")
    assert markdown.count("![") == 7
    assert "无优胜项" in markdown
    assert "完整 status ledger 的 canonical/physical 身份链与 final 双哈希均已闭合" in markdown
    assert "Terminal raw-evidence registry canonical / physical SHA-256" in markdown
    assert "筛选基线 PassRegistry canonical / physical SHA-256" in markdown
    assert "可执行 PassRegistry canonical / physical SHA-256" in markdown
    assert "预期不同" in markdown
    assert "Repository commit / tree" in markdown
    assert "OpenJDK 21.0.8" in markdown
    assert "B2 调优证据（不按收益淘汰）" in markdown
    assert "整个 ignored-tree 与迁移 source 的等价性仍未证明" in markdown
    for csv_name, fields in report._CANDIDATE_REPORT_CSV_FIELDS.items():
        payload = (first / csv_name).read_bytes()
        assert b"\r" not in payload
        assert payload.decode("utf-8").splitlines()[0] == ",".join(fields)


def test_r7_rehash_rejects_a_tampered_enumerated_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_path, workspace, campaign, runs = _r7_fixture(tmp_path)
    frozen = report.read_json(freeze_path)
    labels: list[str] = []
    real_resolve = report.resolve_without_symlinks

    def traced_resolve(path: Path, *, label: str) -> Path:
        labels.append(label)
        return real_resolve(path, label=label)

    monkeypatch.setattr(report, "resolve_without_symlinks", traced_resolve)
    assert report._verify_r7_physical_bindings(
        frozen,
        workspace_root=workspace,
        campaign_root=campaign,
        runs_root=runs,
    ) == 56
    assert labels[:3] == [
        "r7 repository root",
        "r7 campaign root",
        "r7 runs root",
    ]
    assert len(labels) == 59
    assert len({label for label in labels if label.startswith("r7 artifact ")}) == 56
    registry = campaign / "run-evidence.tsv"
    same_bytes = campaign / "same-bytes.tsv"
    atomic_write_text(same_bytes, "registered\n")
    registry.unlink()
    registry.symlink_to(same_bytes)
    with pytest.raises(ValidationError, match="symbolic link"):
        report._verify_r7_physical_bindings(
            frozen,
            workspace_root=workspace,
            campaign_root=campaign,
            runs_root=runs,
        )
    registry.unlink()
    atomic_write_text(campaign / "run-evidence.tsv", "tampered\n")
    with pytest.raises(ValidationError, match="physical artifact hash differs"):
        report._verify_r7_physical_bindings(
            frozen,
            workspace_root=workspace,
            campaign_root=campaign,
            runs_root=runs,
        )


def test_candidate_final_cli_dispatches_terminal_report_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def input_file(name: str) -> Path:
        path = workspace / f"{name}.json"
        atomic_write_json(path, {"input": name})
        return path

    screening_path = input_file("screening")
    registry_path = input_file("registry")
    matrix_path = input_file("matrix")
    campaign_plan_path = input_file("campaign-plan")
    pre_final_status_path = input_file("pre-final-status")
    pre_final_ledger_path = input_file("pre-final-ledger")
    terminal_status_path = input_file("terminal-status")
    terminal_ledger_paths = [
        input_file("terminal-ledger-0"),
        input_file("terminal-ledger-1"),
    ]
    b2_study_path = input_file("b2-study")
    suite_study_path = input_file("b3-study")
    diagnostic_study_path = input_file("diagnostic-study")
    freeze_path = input_file("freeze")
    r7_freeze_path = input_file("r7-freeze")
    raw_run_path = input_file("raw-run")
    r7_campaign_root = workspace / "r7-campaign"
    r7_runs_root = workspace / "r7-runs"
    r7_campaign_root.mkdir()
    r7_runs_root.mkdir()
    final_output_path = workspace / "candidate-final.json"
    report_output_directory = workspace / "candidate-report"
    top3 = ["candidate-a", "candidate-b", "candidate-c"]
    final = {
        "schema_version": "candidate-final.v1",
        "ranking": [],
        "winner_candidate_id": None,
        "diagnostics": {"top3_candidate_ids": top3},
    }
    cli._publish_immutable_json(
        final_output_path, final, label="candidate final fixture"
    )
    original_final_bytes = final_output_path.read_bytes()
    original_final_mtime_ns = final_output_path.stat().st_mtime_ns
    final_calls: list[dict[str, Any]] = []
    report_calls: list[dict[str, Any]] = []

    def fake_final(**arguments: Any) -> dict[str, Any]:
        final_calls.append(arguments)
        return deepcopy(final)

    def fake_report(**arguments: Any) -> dict[str, Any]:
        report_calls.append(arguments)
        return {"schema_version": "candidate-report.v1"}

    monkeypatch.setattr(cli, "build_candidate_final", fake_final)
    monkeypatch.setattr(cli, "build_candidate_report", fake_report)
    required_run_tasks = [
        "run.B1.full",
        "run.B3.full",
        "run.B3.gcc",
        "run.B3.clang",
        "diagnostic.cache.full",
        *(f"diagnostic.cache.{candidate_id}" for candidate_id in top3),
    ]
    arguments = [
        "candidates",
        "final",
        "--workspace-root",
        str(workspace),
        "--screening",
        str(screening_path),
        "--registry",
        str(registry_path),
        "--matrix",
        str(matrix_path),
        "--campaign-plan",
        str(campaign_plan_path),
        "--campaign-status",
        str(pre_final_status_path),
        "--status-ledger",
        str(pre_final_ledger_path),
        "--b2-study",
        str(b2_study_path),
        "--study",
        f"B3={suite_study_path}",
        "--diagnostic-study",
        str(diagnostic_study_path),
        "--freeze",
        str(freeze_path),
        "--final-id",
        "candidate-final:test",
        "--output",
        str(final_output_path),
        "--report-output-dir",
        str(report_output_directory),
        "--report-campaign-status",
        str(terminal_status_path),
        "--r7-freeze",
        str(r7_freeze_path),
        "--r7-campaign-root",
        str(r7_campaign_root),
        "--r7-runs-root",
        str(r7_runs_root),
    ]
    for ledger_path in terminal_ledger_paths:
        arguments.extend(("--report-status-ledger", str(ledger_path)))
    for task_id in required_run_tasks:
        arguments.extend(("--run", f"{task_id}={raw_run_path}"))

    assert cli.main(arguments) == 0
    assert final_output_path.read_bytes() == original_final_bytes
    assert final_output_path.stat().st_mtime_ns == original_final_mtime_ns
    assert json.loads(capsys.readouterr().out) == {
        "eligible": 0,
        "report_schema_version": "candidate-report.v1",
        "schema_version": "candidate-final.v1",
    }
    assert len(final_calls) == len(report_calls) == 1
    assert final_calls[0]["campaign_status_path"] == pre_final_status_path
    assert final_calls[0]["status_ledger_paths"] == [pre_final_ledger_path]
    called = report_calls[0]
    assert called["candidate_final_path"] == final_output_path
    assert called["campaign_plan_path"] == campaign_plan_path
    assert called["completed_campaign_status_path"] == terminal_status_path
    assert called["completed_status_ledger_paths"] == terminal_ledger_paths
    assert called["b1_full_run_path"] == raw_run_path
    assert called["full_run_path"] == raw_run_path
    assert called["winner_run_path"] is None
    assert called["diagnostic_study_path"] == diagnostic_study_path
    assert called["comparison_paths"] == {
        "gcc-13.3-o2": raw_run_path,
        "clang-18-o3": raw_run_path,
    }
    assert called["hotblock_run_paths"] == {
        "cache-full": raw_run_path,
        **{f"cache-{candidate_id}": raw_run_path for candidate_id in top3},
    }
    assert called["r7_campaign_root"] == r7_campaign_root
    assert called["r7_runs_root"] == r7_runs_root

    partial_report_arguments = list(arguments)
    report_output_index = partial_report_arguments.index("--report-output-dir")
    del partial_report_arguments[report_output_index : report_output_index + 2]
    final_call_count = len(final_calls)
    assert cli.main(partial_report_arguments) == 2
    assert len(final_calls) == final_call_count
    capsys.readouterr()

    final_output_path.write_bytes(b"victim-final-must-not-change\n")
    report_call_count = len(report_calls)
    assert cli.main(arguments) == 2
    assert final_output_path.read_bytes() == b"victim-final-must-not-change\n"
    assert len(report_calls) == report_call_count
