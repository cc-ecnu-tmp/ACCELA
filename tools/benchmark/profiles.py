from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Any, Sequence

from .errors import ConfigurationError, ValidationError
from .schema import load_and_validate, validate_document
from .util import atomic_write_json, atomic_write_text, safe_slug, sha256_bytes, sha256_json


def _profile_bytes(profile: dict[str, Any]) -> bytes:
    # Matches PipelineProfile.toJson field ordering and compact representation.
    return (json.dumps(profile, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _profile(base: str, disabled_passes: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "base": base,
        "disable": [{"pass": pass_id} for pass_id in disabled_passes],
        "enable_candidates": [],
    }


def generate_ablation_profiles(
    *,
    registry_path: Path,
    output_directory: Path,
    top_pairs: Sequence[tuple[str, str]] = (),
    top_families: Sequence[str] = (),
) -> dict[str, Any]:
    registry = load_and_validate(registry_path)
    if registry["schema_version"] != "pass-registry.v2":
        raise ValidationError("ablation profiles require a pass-registry.v2 snapshot")
    if len(top_families) > 5 or len(set(top_families)) != len(top_families):
        raise ConfigurationError("Top-family selection must contain at most five unique families")
    requested_pairs = [*top_pairs, *combinations(top_families, 2)]
    if len(requested_pairs) > 10:
        raise ConfigurationError("at most ten two-way interaction profiles may be scheduled")

    passes = [item for item in registry["passes"] if item["lifecycle"] != "candidate"]
    family_order: list[str] = []
    family_passes: dict[str, list[dict[str, Any]]] = {}
    for descriptor in passes:
        family = descriptor["logical_family_id"]
        if family not in family_passes:
            family_order.append(family)
            family_passes[family] = []
        family_passes[family].append(descriptor)
    optional_by_family = {
        family: [
            item["id"]
            for item in family_passes[family]
            if item["lifecycle"] != "required"
        ]
        for family in family_order
    }
    unschedulable = [family for family in family_order if not optional_by_family[family]]

    normalized_pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    family_index = {family: index for index, family in enumerate(family_order)}
    for left, right in requested_pairs:
        if left == right:
            raise ConfigurationError("Top-pair families must be distinct")
        if left not in family_passes or right not in family_passes:
            raise ConfigurationError(f"Top-pair references an unknown logical family: {left}+{right}")
        if left in unschedulable or right in unschedulable:
            raise ConfigurationError(f"Top-pair includes a required-only family: {left}+{right}")
        pair = tuple(sorted((left, right), key=family_index.__getitem__))
        if pair in seen_pairs:
            raise ConfigurationError(f"duplicate Top-pair: {left}+{right}")
        seen_pairs.add(pair)
        normalized_pairs.append(pair)

    generated: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def add_profile(profile_id: str, kind: str, families: list[str], profile: dict[str, Any]) -> None:
        filename = safe_slug(profile_id) + ".json"
        payload = _profile_bytes(profile)
        generated.append(
            (
                {
                    "profile_id": profile_id,
                    "kind": kind,
                    "logical_families": families,
                    "profile_sha256": sha256_bytes(payload),
                    "path": f"profiles/{filename}",
                },
                {"profile": profile, "payload": payload},
            )
        )

    add_profile("full", "full", [], _profile("FULL", []))
    add_profile("mandatory", "mandatory", [], _profile("MANDATORY_ONLY", []))
    for family in family_order:
        optional = optional_by_family[family]
        if optional:
            add_profile(f"without.{family}", "family_ablation", [family], _profile("FULL", optional))
    for left, right in normalized_pairs:
        disabled = [
            item["id"]
            for item in passes
            if item["logical_family_id"] in {left, right}
            and item["lifecycle"] != "required"
        ]
        add_profile(
            f"without.{left}+{right}",
            "pair_ablation",
            [left, right],
            _profile("FULL", disabled),
        )

    profile_records = [record for record, _ in generated]
    schedule = [
        {
            "baseline_profile_id": "full",
            "candidate_profile_id": record["profile_id"],
            "kind": (
                "mandatory_control" if record["kind"] == "mandatory" else record["kind"]
            ),
        }
        for record in profile_records
        if record["profile_id"] != "full"
    ]
    matrix = validate_document(
        {
            "schema_version": "ablation-matrix.v1",
            "registry_sha256": sha256_json(registry),
            "profiles": profile_records,
            "schedule": schedule,
            "unschedulable_families": unschedulable,
        }
    )

    output = output_directory.resolve()
    expected_files = {record["path"] for record in profile_records} | {"matrix.json"}
    if output.exists():
        existing_files = {
            path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
        }
        unexpected = sorted(existing_files - expected_files)
        if unexpected:
            raise ConfigurationError(
                "ablation output directory contains stale/unmanaged files: " + ", ".join(unexpected[:10])
            )
    output.mkdir(parents=True, exist_ok=True)
    for record, generated_profile in generated:
        atomic_write_text(
            output / Path(record["path"]),
            generated_profile["payload"].decode("utf-8"),
        )
    atomic_write_json(output / "matrix.json", matrix)
    return matrix
