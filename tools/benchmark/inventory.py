from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import ValidationError
from .schema import load_and_validate, validate_document
from .util import file_ref, read_json, resolve_manifest_path, sha256_file, sha256_json, validate_relative_path

_NUMERIC_VARIANT = re.compile(r"^(.+?)(?:[-_]?\d+)$")


def derive_family(stem: str) -> str:
    match = _NUMERIC_VARIANT.fullmatch(stem)
    if match and match.group(1).rstrip("-_"):
        return match.group(1).rstrip("-_")
    return stem


def _provenance(
    *,
    data_role: str,
    origin_source: str,
    origin_snapshot_sha256: str,
    license_expression: str,
    validity_status: str = "included",
    validity_reason: str = "verified",
    derived_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "data_role": data_role,
        "validity": {"status": validity_status, "reason": validity_reason},
        "origin": {
            "source_id": origin_source,
            "snapshot_sha256": origin_snapshot_sha256,
        },
        "license": {"spdx_expression": license_expression},
        "derived_from": None if derived_from is None else dict(derived_from),
    }


def _build_manifest(
    *,
    suite_id: str,
    provenance: Mapping[str, Any],
    cases: Sequence[dict[str, Any]],
    orphan_sidecars: Sequence[dict[str, Any]] = (),
    captured_at: str | None = None,
) -> dict[str, Any]:
    if not cases:
        raise ValidationError("manifest selection contains no benchmark cases")

    # A source can legitimately be paired with several input datasets.  The
    # logical-file inventory is path based so that this reuse is not mistaken
    # for several physical files.
    logical_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for case in cases:
        for field, role in (("source", "source"), ("input", "input"), ("expected_output", "expected_output")):
            reference = case[field]
            if reference is None:
                continue
            path = Path(reference["path"])
            item = {
                "logical_id": path.with_suffix("").as_posix(),
                "suffix": path.suffix,
                "role": role,
                "sha256": reference["sha256"],
                "size_bytes": reference["size_bytes"],
                "provenance": deepcopy(case["provenance"]),
            }
            key = (item["logical_id"], item["suffix"], role)
            previous = logical_by_key.get(key)
            if previous is not None and previous != item:
                raise ValidationError(f"logical file has conflicting metadata: {item['logical_id']}{item['suffix']}")
            logical_by_key[key] = item
    for item in orphan_sidecars:
        key = (item["logical_id"], item["suffix"], item["role"])
        if key in logical_by_key:
            raise ValidationError(f"orphan duplicates a claimed logical file: {item['logical_id']}{item['suffix']}")
        logical_by_key[key] = deepcopy(item)

    by_content: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for item in logical_by_key.values():
        by_content.setdefault((item["sha256"], item["size_bytes"]), []).append(item)
    duplicate_file_groups = [
        {
            "sha256": digest,
            "size_bytes": size,
            "members": sorted(members, key=lambda item: (item["logical_id"], item["suffix"], item["role"])),
        }
        for (digest, size), members in sorted(by_content.items())
        if len(members) >= 2
    ]

    grouped_sources: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        grouped_sources.setdefault(case["source_group"], []).append(case)
    source_groups = [
        {
            "source_group": source_group,
            "source_sha256": group[0]["source"]["sha256"],
            "case_count": len(group),
            "members": sorted(case["id"] for case in group),
            "families": sorted({case["family"] for case in group}),
        }
        for source_group, group in sorted(grouped_sources.items())
    ]
    grouped_families: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in cases:
        grouped_families.setdefault((case["family"], case["target"]), []).append(case)
    family_groups = [
        {
            "family": family,
            "target": group_target,
            "case_count": len(group),
            "distinct_source_groups": len({case["source_group"] for case in group}),
        }
        for (family, group_target), group in sorted(grouped_families.items())
    ]

    manifest_provenance = deepcopy(provenance)
    if orphan_sidecars and manifest_provenance["validity"]["status"] == "included":
        manifest_provenance["validity"] = {
            "status": "included_with_exclusions",
            "reason": "packaging_defect",
        }
    manifest = {
        "schema_version": "benchmark-manifest.v1",
        "suite_id": suite_id,
        "provenance": manifest_provenance,
        "cases": list(cases),
        "data_quality": {
            "orphan_count": len(orphan_sidecars),
            "duplicate_group_count": len(duplicate_file_groups),
            "source_group_count": len(source_groups),
            "orphan_sidecars": list(orphan_sidecars),
            "duplicate_file_groups": duplicate_file_groups,
            "source_groups": source_groups,
            "family_groups": family_groups,
        },
    }
    if captured_at is not None:
        manifest["captured_at"] = captured_at
    return validate_document(manifest)


def inventory_suite(
    suite_root: Path,
    *,
    suite_id: str,
    target: str,
    data_role: str,
    origin_source: str,
    origin_snapshot_sha256: str,
    license_expression: str,
    validity_status: str = "included",
    validity_reason: str = "verified",
    captured_at: str | None = None,
    source_suffix: str = ".sy",
    ignore_orphans: bool = False,
) -> dict[str, Any]:
    if target != "rv64gc":
        raise ValidationError("ACCELA benchmark manifests require target=rv64gc")
    root = suite_root.resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("suite root must be a directory")
    if not source_suffix.startswith(".") or "/" in source_suffix or "\\" in source_suffix:
        raise ValidationError("source suffix must be a simple extension such as .sy")

    provenance = _provenance(
        data_role=data_role,
        origin_source=origin_source,
        origin_snapshot_sha256=origin_snapshot_sha256,
        license_expression=license_expression,
        validity_status=validity_status,
        validity_reason=validity_reason,
    )
    case_provenance = {
        **deepcopy(provenance),
        "validity": {"status": "included", "reason": "verified"},
    }

    sources = sorted(path for path in root.rglob(f"*{source_suffix}") if path.is_file())
    if not sources:
        raise ValidationError(f"suite contains no {source_suffix} source files")

    cases: list[dict[str, Any]] = []
    claimed_sidecars: set[Path] = set()
    for source in sources:
        expected = source.with_suffix(".out")
        input_path = source.with_suffix(".in")
        if not expected.is_file():
            raise ValidationError(f"missing expected output for {source.relative_to(root).as_posix()}")
        if input_path.exists() and not input_path.is_file():
            raise ValidationError(f"input sidecar is not a regular file: {input_path.relative_to(root).as_posix()}")
        relative_source = source.relative_to(root)
        logical_name = relative_source.with_suffix("").as_posix()
        case_id = f"{target}:{logical_name}"
        source_reference = file_ref(source, root)
        cases.append(
            {
                "id": case_id,
                "family": derive_family(source.stem),
                "source_group": f"sg-{source_reference['sha256']}",
                "target": target,
                "weight": 1.0,
                "source": source_reference,
                "input": file_ref(input_path, root) if input_path.is_file() else None,
                "expected_output": file_ref(expected, root),
                "tags": [relative_source.parent.as_posix()] if relative_source.parent != Path(".") else [],
                "provenance": deepcopy(case_provenance),
            }
        )
        claimed_sidecars.add(expected.resolve())
        if input_path.is_file():
            claimed_sidecars.add(input_path.resolve())

    orphan_paths = sorted(
        path
        for suffix in (".in", ".out")
        for path in root.rglob(f"*{suffix}")
        if path.is_file() and path.resolve() not in claimed_sidecars
    )
    orphan_sidecars = [
        {
            "logical_id": path.relative_to(root).with_suffix("").as_posix(),
            "suffix": path.suffix,
            "role": "orphan",
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "provenance": {
                **deepcopy(provenance),
                "validity": {"status": "excluded", "reason": "packaging_defect"},
            },
        }
        for path in orphan_paths
    ]
    if orphan_paths and not ignore_orphans:
        relative_orphans = [path.relative_to(root).as_posix() for path in orphan_paths]
        preview = ", ".join(relative_orphans[:10])
        extra = f" and {len(relative_orphans) - 10} more" if len(relative_orphans) > 10 else ""
        raise ValidationError(f"orphan input/output sidecars: {preview}{extra}")

    return _build_manifest(
        suite_id=suite_id,
        provenance=provenance,
        cases=cases,
        orphan_sidecars=orphan_sidecars,
        captured_at=captured_at,
    )


def _selected(value: str, filters: set[str]) -> bool:
    return not filters or value in filters


def _corpus_file(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise ValidationError(f"clean-room {label} must be a relative path string")
    validate_relative_path(value, label=f"clean-room {label}")
    return resolve_manifest_path(root, value)


def inventory_cleanroom_manifest(
    corpus_manifest_path: Path,
    *,
    suite_id: str,
    target: str,
    data_role: str,
    origin_source: str,
    origin_snapshot_sha256: str | None = None,
    license_expression: str | None = None,
    tiers: Sequence[str] = (),
    families: Sequence[str] = (),
    dataset_roles: Sequence[str] = (),
    oracle_legs: Sequence[str] = ("baseline", "optimized"),
    captured_at: str | None = None,
) -> dict[str, Any]:
    if target != "rv64gc":
        raise ValidationError("ACCELA benchmark manifests require target=rv64gc")
    """Normalize the repository clean-room corpus into benchmark-manifest.v1.

    B5 imports the 20-family structural-variant corpus, B6 imports mature
    PolyBench/Embench-style programs and selected datasets, and oracle imports both source legs of every
    selected semantic pair.  No path outside the corpus root can be referenced.
    """

    manifest_path = corpus_manifest_path.resolve(strict=True)
    root = manifest_path.parent
    raw = read_json(manifest_path)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValidationError("clean-room manifest must use schema_version 1")
    required_top = {"provenance_policy", "benchmarks", "oracle_families", "structural_variants"}
    if not required_top.issubset(raw):
        raise ValidationError("clean-room manifest lacks required corpus sections")
    if data_role not in {"B5", "B6", "oracle"}:
        raise ValidationError("clean-room import data_role must be B5, B6, or oracle")
    snapshot = sha256_file(manifest_path)
    if origin_snapshot_sha256 is not None and origin_snapshot_sha256 != snapshot:
        raise ValidationError("declared clean-room snapshot SHA-256 does not match manifest bytes")
    policy = raw["provenance_policy"]
    if not isinstance(policy, dict) or not isinstance(policy.get("license"), str):
        raise ValidationError("clean-room manifest lacks a provenance_policy license")
    corpus_license = policy["license"]
    if license_expression is not None and license_expression != corpus_license:
        raise ValidationError("declared license does not match clean-room corpus policy")
    provenance = _provenance(
        data_role=data_role,
        origin_source=origin_source,
        origin_snapshot_sha256=snapshot,
        license_expression=corpus_license,
    )
    tier_filter = set(tiers)
    family_filter = set(families)
    role_filter = set(dataset_roles)
    if len(tier_filter) != len(tiers) or len(family_filter) != len(families):
        raise ValidationError("clean-room filters must not contain duplicates")
    legs = tuple(oracle_legs)
    if len(set(legs)) != len(legs) or not legs or any(leg not in {"baseline", "optimized"} for leg in legs):
        raise ValidationError("oracle legs must be a non-empty unique subset of baseline/optimized")

    cases: list[dict[str, Any]] = []

    def add_case(
        *,
        case_id: str,
        family: str,
        source_value: object,
        input_value: object | None,
        output_value: object,
        tags: Iterable[str],
        oracle_pair: dict[str, str] | None = None,
    ) -> None:
        source = _corpus_file(root, source_value, label=f"{case_id} source")
        expected = _corpus_file(root, output_value, label=f"{case_id} output")
        input_path = None if input_value is None else _corpus_file(root, input_value, label=f"{case_id} input")
        source_reference = file_ref(source, root)
        case: dict[str, Any] = {
            "id": case_id,
            "family": family,
            "source_group": f"sg-{source_reference['sha256']}",
            "target": target,
            "weight": 1.0,
            "source": source_reference,
            "input": None if input_path is None else file_ref(input_path, root),
            "expected_output": file_ref(expected, root),
            "tags": sorted(set(tags)),
            "provenance": deepcopy(provenance),
        }
        if oracle_pair is not None:
            case["oracle_pair"] = oracle_pair
        cases.append(case)

    if data_role == "B6":
        entries = raw["benchmarks"]
        if not isinstance(entries, list):
            raise ValidationError("clean-room benchmarks must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("clean-room benchmark entry must be an object")
            benchmark_id = entry.get("id")
            group = entry.get("group")
            if not isinstance(benchmark_id, str) or not isinstance(group, str):
                raise ValidationError("clean-room benchmark lacks id/group")
            if not _selected(benchmark_id, family_filter):
                continue
            datasets = entry.get("datasets")
            if not isinstance(datasets, list):
                raise ValidationError(f"clean-room benchmark lacks datasets: {benchmark_id}")
            for dataset in datasets:
                if not isinstance(dataset, dict):
                    raise ValidationError(f"clean-room dataset is not an object: {benchmark_id}")
                tier = dataset.get("tier")
                role = dataset.get("role")
                if not isinstance(tier, str) or not isinstance(role, str):
                    raise ValidationError(f"clean-room dataset lacks tier/role: {benchmark_id}")
                if not _selected(tier, tier_filter) or not _selected(role, role_filter):
                    continue
                add_case(
                    case_id=f"{target}:{benchmark_id}:{tier}",
                    family=benchmark_id,
                    source_value=entry.get("source"),
                    input_value=dataset.get("input"),
                    output_value=dataset.get("output"),
                    tags=(f"group:{group}", f"tier:{tier}", f"dataset-role:{role}"),
                )
    elif data_role == "B5":
        if tier_filter or role_filter:
            raise ValidationError("B5 structural variants do not support tier/dataset-role filters")
        entries = raw["structural_variants"]
        if not isinstance(entries, list):
            raise ValidationError("clean-room structural_variants must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("clean-room structural variant must be an object")
            family = entry.get("family_taxonomy")
            kind = entry.get("variant_kind")
            if not isinstance(family, str) or not isinstance(kind, str):
                raise ValidationError("clean-room structural variant lacks family_taxonomy/variant_kind")
            if not _selected(family, family_filter):
                continue
            add_case(
                case_id=f"{target}:{family}:{kind}",
                family=family,
                source_value=entry.get("source"),
                input_value=entry.get("input"),
                output_value=entry.get("output"),
                tags=("structural-variant", f"variant-kind:{kind}"),
            )
    else:
        if role_filter:
            raise ValidationError("oracle datasets do not support dataset-role filters")
        entries = raw["oracle_families"]
        if not isinstance(entries, list):
            raise ValidationError("clean-room oracle_families must be an array")
        pending_pairs: dict[str, dict[str, str]] = {}
        for family_entry in entries:
            if not isinstance(family_entry, dict) or not isinstance(family_entry.get("family"), str):
                raise ValidationError("clean-room oracle family lacks family")
            family = family_entry["family"]
            if not _selected(family, family_filter):
                continue
            variants = family_entry.get("variants")
            if not isinstance(variants, list):
                raise ValidationError(f"clean-room oracle family lacks variants: {family}")
            for variant_entry in variants:
                if not isinstance(variant_entry, dict) or not isinstance(variant_entry.get("variant"), str):
                    raise ValidationError(f"clean-room oracle variant is malformed: {family}")
                variant = variant_entry["variant"]
                datasets = variant_entry.get("datasets")
                if not isinstance(datasets, list):
                    raise ValidationError(f"clean-room oracle variant lacks datasets: {family}/{variant}")
                for dataset in datasets:
                    if not isinstance(dataset, dict) or not isinstance(dataset.get("tier"), str):
                        raise ValidationError(f"clean-room oracle dataset is malformed: {family}/{variant}")
                    tier = dataset["tier"]
                    if not _selected(tier, tier_filter):
                        continue
                    pair_id = f"{family}:{variant}:{tier}"
                    pair_cases = {
                        leg: f"{target}:oracle:{family}:{variant}:{tier}:{leg}"
                        for leg in ("baseline", "optimized")
                    }
                    for leg in legs:
                        case_id = pair_cases[leg]
                        add_case(
                            case_id=case_id,
                            family=family,
                            source_value=variant_entry.get(leg),
                            input_value=dataset.get("input"),
                            output_value=dataset.get("output"),
                            tags=(f"tier:{tier}", f"oracle-leg:{leg}", f"oracle-variant:{variant}"),
                            oracle_pair={"pair_id": pair_id, "leg": leg, "counterpart_case_id": case_id},
                        )
                    pending_pairs[pair_id] = pair_cases
        for case in cases:
            pairing = case["oracle_pair"]
            counterpart = "optimized" if pairing["leg"] == "baseline" else "baseline"
            pair_cases = pending_pairs[pairing["pair_id"]]
            pairing["counterpart_case_id"] = pair_cases[counterpart]

    requested_families = family_filter
    observed_families = {case["family"] for case in cases}
    missing_families = sorted(requested_families - observed_families)
    if missing_families:
        raise ValidationError("clean-room family filter matched nothing: " + ", ".join(missing_families))
    return _build_manifest(
        suite_id=suite_id,
        provenance=provenance,
        cases=cases,
        captured_at=captured_at,
    )


def subset_manifest(
    source_manifest_path: Path,
    *,
    suite_root: Path,
    suite_id: str,
    case_ids: Sequence[str],
    data_role: str,
    origin_source: str,
    origin_snapshot_sha256: str | None = None,
    license_expression: str | None = None,
    require_one_per_family: bool = False,
    captured_at: str | None = None,
) -> dict[str, Any]:
    source = load_and_validate(source_manifest_path, suite_root=suite_root, verify_files=True)
    if source["schema_version"] != "benchmark-manifest.v1":
        raise ValidationError("manifest subset source must be benchmark-manifest.v1")
    if data_role == "B2" and source["provenance"]["data_role"] != "B3":
        raise ValidationError("B2 must be derived from an explicitly inventoried B3 manifest")
    source_manifest_sha256 = sha256_json(source)
    source_license = source["provenance"]["license"]["spdx_expression"]
    if origin_snapshot_sha256 is not None and origin_snapshot_sha256 != source_manifest_sha256:
        raise ValidationError("subset origin snapshot must equal the canonical parent manifest SHA-256")
    if license_expression is not None and license_expression != source_license:
        raise ValidationError("subset license must match the parent manifest license")
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValidationError("manifest subset case ids must be a non-empty unique list")
    by_id = {case["id"]: case for case in source["cases"]}
    unknown = [case_id for case_id in case_ids if case_id not in by_id]
    if unknown:
        raise ValidationError("manifest subset references unknown case ids: " + ", ".join(unknown[:10]))
    provenance = _provenance(
        data_role=data_role,
        origin_source=origin_source,
        origin_snapshot_sha256=source_manifest_sha256,
        license_expression=source_license,
        derived_from={
            "suite_id": source["suite_id"],
            "manifest_sha256": source_manifest_sha256,
            "origin_source_id": source["provenance"]["origin"]["source_id"],
            "origin_snapshot_sha256": source["provenance"]["origin"]["snapshot_sha256"],
        },
    )
    selected = []
    for case_id in case_ids:
        case = deepcopy(by_id[case_id])
        case["provenance"] = deepcopy(provenance)
        selected.append(case)
    if require_one_per_family:
        source_families = {case["family"] for case in source["cases"]}
        selected_counts = {family: 0 for family in source_families}
        for case in selected:
            selected_counts[case["family"]] += 1
        invalid = sorted(family for family, count in selected_counts.items() if count != 1)
        if invalid:
            raise ValidationError(
                "one-per-family subset requirement failed for: " + ", ".join(invalid[:20])
            )
    return _build_manifest(
        suite_id=suite_id,
        provenance=provenance,
        cases=selected,
        captured_at=captured_at,
    )
