from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigurationError, ValidationError
from .inventory import _build_manifest
from .schema import load_and_validate, validate_document
from .util import atomic_write_json, sha256_json


def build_oracle_plan(
    *,
    manifest_path: Path,
    suite_root: Path,
    pipeline_profile_id: str,
    pipeline_profile_sha256: str,
    baseline_run_id: str,
    optimized_run_id: str,
) -> dict[str, Any]:
    """Expand reciprocal clean-room source pairs under one compiler pipeline."""

    manifest = load_and_validate(manifest_path, suite_root=suite_root, verify_files=True)
    if manifest["schema_version"] != "benchmark-manifest.v1":
        raise ValidationError("oracle planning requires benchmark-manifest.v1")
    data_role = manifest["provenance"]["data_role"]
    evidence_by_role = {
        "oracle": "cleanroom",
        "B3": "official",
        "B4": "holdout_or_mature",
        "B6": "holdout_or_mature",
    }
    evidence_class = evidence_by_role.get(data_role)
    if evidence_class is None:
        raise ConfigurationError(
            "oracle planning requires a paired oracle/B3/B4/B6 manifest; evidence class is derived from data_role"
        )
    legacy_ids = {
        f"oracle-baseline:{manifest['suite_id']}",
        f"oracle-optimized:{manifest['suite_id']}",
    }
    if baseline_run_id == optimized_run_id:
        raise ConfigurationError("oracle source legs require distinct explicit run ids")
    if {baseline_run_id, optimized_run_id} & legacy_ids:
        raise ConfigurationError(
            "oracle planning rejects legacy deterministic run ids; declare fresh campaign-bound ids"
        )

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for case in manifest["cases"]:
        validity = case["provenance"]["validity"]
        if validity["status"] != "included" or validity["reason"] != "verified":
            raise ConfigurationError(f"oracle case is not verified/included: {case['id']}")
        pairing = case.get("oracle_pair")
        if pairing is None:
            raise ConfigurationError(f"oracle case lacks paired-source metadata: {case['id']}")
        legs = grouped.setdefault(pairing["pair_id"], {})
        if pairing["leg"] in legs:
            raise ConfigurationError(f"oracle pair repeats a source leg: {pairing['pair_id']}")
        legs[pairing["leg"]] = case

    pairs: list[dict[str, Any]] = []
    for pair_id, legs in sorted(grouped.items()):
        if set(legs) != {"baseline", "optimized"}:
            raise ConfigurationError(f"oracle pair requires baseline and optimized sources: {pair_id}")
        baseline = legs["baseline"]
        optimized = legs["optimized"]
        if baseline["oracle_pair"]["counterpart_case_id"] != optimized["id"] or optimized["oracle_pair"]["counterpart_case_id"] != baseline["id"]:
            raise ConfigurationError(f"oracle pair links are not reciprocal: {pair_id}")
        if baseline["target"] != optimized["target"] or baseline["family"] != optimized["family"]:
            raise ConfigurationError(f"oracle pair target/family mismatch: {pair_id}")
        if baseline["input"] != optimized["input"] or baseline["expected_output"] != optimized["expected_output"]:
            raise ConfigurationError(f"oracle pair input/output bytes differ: {pair_id}")
        pairs.append(
            {
                "pair_id": pair_id,
                "family": baseline["family"],
                "target": baseline["target"],
                "input_sha256": None if baseline["input"] is None else baseline["input"]["sha256"],
                "expected_output_sha256": baseline["expected_output"]["sha256"],
                "baseline": {
                    "case_id": baseline["id"],
                    "source_group": baseline["source_group"],
                    "source_sha256": baseline["source"]["sha256"],
                },
                "optimized": {
                    "case_id": optimized["id"],
                    "source_group": optimized["source_group"],
                    "source_sha256": optimized["source"]["sha256"],
                },
            }
        )

    return validate_document(
        {
            "schema_version": "oracle-plan.v1",
            "evidence_class": evidence_class,
            "manifest_data_role": data_role,
            "suite_id": manifest["suite_id"],
            "manifest_sha256": sha256_json(manifest),
            "pipeline_profile": {
                "profile_id": pipeline_profile_id,
                "profile_sha256": pipeline_profile_sha256,
            },
            "baseline_run_id": baseline_run_id,
            "optimized_run_id": optimized_run_id,
            "pairs": pairs,
        }
    )


def prepare_oracle_leg_manifest(
    *,
    plan_path: Path,
    manifest_path: Path,
    suite_root: Path,
    leg: Literal["baseline", "optimized"],
    run_id: str | None,
    pipeline_profile_id: str,
    pipeline_profile_sha256: str,
    output_path: Path,
) -> dict[str, Any]:
    """Verify a plan and materialize exactly one schedulable source leg."""

    plan = load_and_validate(plan_path)
    if plan["schema_version"] != "oracle-plan.v1":
        raise ValidationError("oracle run requires oracle-plan.v1")
    manifest = load_and_validate(manifest_path, suite_root=suite_root, verify_files=True)
    if sha256_json(manifest) != plan["manifest_sha256"] or manifest["suite_id"] != plan["suite_id"]:
        raise ConfigurationError("oracle plan does not describe the supplied manifest snapshot")
    profile = plan["pipeline_profile"]
    if profile["profile_id"] != pipeline_profile_id or profile["profile_sha256"] != pipeline_profile_sha256:
        raise ConfigurationError("oracle run pipeline profile differs from the paired plan")
    expected_run_id = plan[f"{leg}_run_id"]
    if run_id != expected_run_id:
        raise ConfigurationError(f"oracle {leg} run-id must be {expected_run_id}")

    by_id = {case["id"]: case for case in manifest["cases"]}
    selected: list[dict[str, Any]] = []
    for pair in plan["pairs"]:
        descriptor = pair[leg]
        case = by_id.get(descriptor["case_id"])
        if case is None:
            raise ConfigurationError(f"oracle plan references a missing {leg} case: {pair['pair_id']}")
        if case["source_group"] != descriptor["source_group"] or case["source"]["sha256"] != descriptor["source_sha256"]:
            raise ConfigurationError(f"oracle source hash changed after planning: {pair['pair_id']}")
        selected.append(deepcopy(case))
    leg_manifest = _build_manifest(
        suite_id=f"{manifest['suite_id']}-{leg}",
        provenance=manifest["provenance"],
        cases=selected,
    )
    atomic_write_json(output_path, leg_manifest)
    return leg_manifest
