from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .schema import load_and_validate, validate_document
from .util import sha256_json


def _signature(case: Mapping[str, Any]) -> tuple[str, str | None, str]:
    return (
        case["source"]["sha256"],
        None if case["input"] is None else case["input"]["sha256"],
        case["expected_output"]["sha256"],
    )


def build_cross_suite_audit(
    *,
    left_path: Path,
    right_path: Path,
    left_root: Path | None = None,
    right_root: Path | None = None,
) -> dict[str, Any]:
    left = load_and_validate(left_path, suite_root=left_root, verify_files=left_root is not None)
    right = load_and_validate(right_path, suite_root=right_root, verify_files=right_root is not None)
    if left["schema_version"] != "benchmark-manifest.v1" or right["schema_version"] != "benchmark-manifest.v1":
        raise ValidationError("cross-suite audit inputs must be benchmark-manifest.v1")
    left_groups: dict[tuple[str, str | None, str], list[str]] = {}
    right_groups: dict[tuple[str, str | None, str], list[str]] = {}
    for case in left["cases"]:
        left_groups.setdefault(_signature(case), []).append(case["id"])
    for case in right["cases"]:
        right_groups.setdefault(_signature(case), []).append(case["id"])
    mappings: list[dict[str, Any]] = []
    for signature in sorted(set(left_groups) | set(right_groups), key=lambda value: tuple(item or "" for item in value)):
        left_ids = sorted(left_groups.get(signature, ()))
        right_ids = sorted(right_groups.get(signature, ()))
        matched_count = min(len(left_ids), len(right_ids))
        left_only_count = len(left_ids) - matched_count
        right_only_count = len(right_ids) - matched_count
        if left_ids and right_ids:
            status = "identical" if len(left_ids) == len(right_ids) else "multiplicity_mismatch"
        else:
            status = "left_only" if left_ids else "right_only"
        mappings.append(
            {
                "source_sha256": signature[0],
                "input_sha256": signature[1],
                "expected_output_sha256": signature[2],
                "left_case_ids": left_ids,
                "right_case_ids": right_ids,
                "matched_count": matched_count,
                "left_only_count": left_only_count,
                "right_only_count": right_only_count,
                "status": status,
            }
        )
    left_counter = Counter(_signature(case) for case in left["cases"])
    right_counter = Counter(_signature(case) for case in right["cases"])
    identical = left_counter == right_counter
    renamed = identical and sorted(case["id"] for case in left["cases"]) != sorted(
        case["id"] for case in right["cases"]
    )
    return validate_document(
        {
            "schema_version": "cross-suite-audit.v1",
            "left": {"suite_id": left["suite_id"], "manifest_sha256": sha256_json(left)},
            "right": {"suite_id": right["suite_id"], "manifest_sha256": sha256_json(right)},
            "identical_content_multiset": identical,
            "renamed": renamed,
            "counts": {
                "left_cases": len(left["cases"]),
                "right_cases": len(right["cases"]),
                "shared_content_groups": sum(bool(item["matched_count"]) for item in mappings),
                "matched_cases": sum(item["matched_count"] for item in mappings),
                "left_only_cases": sum(item["left_only_count"] for item in mappings),
                "right_only_cases": sum(item["right_only_count"] for item in mappings),
            },
            "mappings": mappings,
        }
    )
