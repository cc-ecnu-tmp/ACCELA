from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ablation import _require_eligible_attempt_history, _require_formal_measurement
from .campaign import (
    build_campaign_environment_contract,
    campaign_run_status,
    campaign_status_chain,
    enforce_terminal_task_immutability,
    ready_campaign_task_ids,
    require_formal_suite_contract,
)
from .errors import ConfigurationError, ValidationError
from .execution import VerifiedRunRawEvidence, verify_run_raw_evidence
from .journal import durable_create_json
from .schema import (
    load_and_validate,
    load_pipeline_profile_v2,
    schema_sha256,
    validate_candidate_remark_jsonl,
    validate_document,
    validate_pipeline_profile_v2,
)
from .stats import (
    bootstrap_geometric_mean_ci,
    case_metric,
    compare_runs,
    family_geometric_means,
    metric_spec,
    weighted_geometric_mean,
)
from .util import (
    canonical_json_bytes,
    read_json,
    resolve_without_symlinks,
    safe_slug,
    sha256_artifact,
    sha256_bytes,
    sha256_file,
    sha256_json,
)


_CANDIDATE_SUITE_CASE_COUNTS = {
    "B1": 140,
    "B2": 20,
    "B3": 60,
    "B4": 59,
    "B5": 60,
    "B6": 88,
}


def _latest_evidence_timestamp(values: Sequence[str]) -> str:
    if not values:
        raise ValidationError("derived candidate artifact lacks terminal evidence time")
    return max(
        values,
        key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _require_candidate_run_protocol(
    run: Mapping[str, Any],
    *,
    data_role: str,
) -> None:
    expected_count = _CANDIDATE_SUITE_CASE_COUNTS[data_role]
    if (
        run["manifest_case_count"] != expected_count
        or len(run["cases"]) != expected_count
        or {case["data_role"] for case in run["cases"]} != {data_role}
    ):
        raise ValidationError(
            f"candidate {data_role} run must bind the exact {expected_count}-case manifest"
        )
    configuration = run["configuration"]
    if (
        configuration["compile_repetitions"] != 5
        or configuration["reuse_compile_cache"]
        or configuration["compile_storage_contract"] != "attempt_local_v1"
        or configuration["max_workers"] != 4
        or configuration["seed"] != 20260809
        or configuration["retry_failures"]
        or configuration["repetitions"] != 1
        or not math.isclose(
            configuration["consistency_fraction"], 0.1, rel_tol=0, abs_tol=1e-12
        )
        or configuration["consistency_repetitions"] != 3
    ):
        raise ValidationError(
            "candidate formal runs require jobs=4, five cold compiles, seed 20260809, "
            "10 percent x3 consistency, no cache, and no retry"
        )


def _require_candidate_correctness_run(
    run: Mapping[str, Any], *, label: str
) -> None:
    """Require the ACCELA BenchmarkCompiler-to-QEMU correctness path for B1."""

    configuration = run["configuration"]
    tool_ids = {item["tool"] for item in configuration["tool_versions"]}
    if (
        configuration["evidence_level"] != "qemu_correctness"
        or configuration["compiler"]["kind"] != "benchmark-compiler"
        or configuration["runner"]["kind"] != "qemu"
        or configuration["output_contract"] != "lf_return_trailer"
        or configuration["pipeline_profile_file_sha256"] is None
        or "qemu-system-riscv64" not in tool_ids
    ):
        raise ValidationError(
            f"{label} must use BenchmarkCompiler, the frozen pipeline, QEMU, "
            "and exact stdout/main-return correctness"
        )
    enabled_candidate_ids = configuration.get("enabled_candidate_ids", [])
    if enabled_candidate_ids and (
        configuration["remarks_file_sha256"] is None
        or any(
            case["artifact_sha256"] is not None
            and (
                case["remarks_sha256"] is None
                or case["remarks_event_count"] is None
                or case["remarks_event_count"] <= 0
                or case.get("candidate_remark_summary") is None
            )
            for case in run["cases"]
        )
    ):
        raise ValidationError(
            f"{label} candidate-enabled correctness requires decision-observable remarks"
        )
    _require_eligible_attempt_history(run, context=f"{label} correctness evidence")


def _load_version(path: Path, version: str, *, label: str) -> dict[str, Any]:
    document = load_and_validate(path)
    if document["schema_version"] != version:
        raise ValidationError(f"{label} must be {version}")
    return document


def _candidate_workspace_root(workspace_root: Path) -> Path:
    root = resolve_without_symlinks(
        workspace_root,
        label="candidate workspace root",
    )
    if not root.is_dir():
        raise ValidationError("candidate workspace root must be a directory")
    return root


def _frozen_artifact_digest(
    workspace_root: Path,
    path: Path,
    document: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, str]:
    root, physical, relative = _workspace_regular_path(
        workspace_root, path, label=label
    )
    return {
        "path": relative.as_posix(),
        "canonical_sha256": sha256_json(document),
        "physical_sha256": sha256_file(physical),
    }


def _workspace_regular_path(
    workspace_root: Path,
    path: Path,
    *,
    label: str,
) -> tuple[Path, Path, Path]:
    """Resolve a workspace file only after rejecting lexical symlink components."""

    root = _candidate_workspace_root(workspace_root)
    lexical = path if path.is_absolute() else root / path
    lexical = lexical.absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must stay within the campaign workspace") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} cannot traverse a symbolic link")
    physical = lexical.resolve(strict=True)
    try:
        physical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} resolves outside the campaign workspace") from exc
    if not physical.is_file():
        raise ValidationError(f"{label} must be a physical regular file")
    return root, physical, relative


def _workspace_directory_path(
    workspace_root: Path,
    path: Path,
    *,
    label: str,
) -> tuple[Path, Path, Path]:
    root = _candidate_workspace_root(workspace_root)
    lexical = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} must stay within the campaign workspace") from exc
    physical = resolve_without_symlinks(lexical, label=label)
    try:
        physical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} resolves outside the campaign workspace") from exc
    if not physical.is_dir():
        raise ValidationError(f"{label} must be a physical directory")
    return root, physical, relative


def _frozen_compiler_artifact(
    workspace_root: Path,
    path: Path,
) -> dict[str, str]:
    root = _candidate_workspace_root(workspace_root)
    lexical = (path if path.is_absolute() else root / path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            "compiler artifact must stay within the campaign workspace"
        ) from exc
    resolved = resolve_without_symlinks(lexical, label="compiler artifact")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError("compiler artifact resolves outside the workspace") from exc
    return {
        "path": relative.as_posix(),
        "physical_sha256": sha256_artifact(lexical),
    }


def _load_frozen_artifact(
    workspace_root: Path,
    artifact: Mapping[str, str],
    *,
    label: str,
    version: str | None = None,
) -> dict[str, Any]:
    root, physical, _ = _workspace_regular_path(
        workspace_root, Path(artifact["path"]), label=label
    )
    if sha256_file(physical) != artifact["physical_sha256"]:
        raise ValidationError(f"{label} physical SHA-256 differs from freeze")
    raw = read_json(physical)
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} must contain a JSON object")
    document = validate_document(raw) if version is not None else raw
    if version is not None and document["schema_version"] != version:
        raise ValidationError(f"{label} schema version differs from freeze")
    if sha256_json(document) != artifact["canonical_sha256"]:
        raise ValidationError(f"{label} canonical SHA-256 differs from freeze")
    return document


def _normalized_remark_summary(
    summary: Mapping[str, Any], *, enabled_candidate_ids: list[str]
) -> dict[str, Any]:
    return {
        "event_count": summary["event_count"],
        "summary_count": summary["summary_count"],
        "paired_candidate_count": summary["paired_candidate_count"],
        "applied_count": summary["applied_count"],
        "rejected_count": summary["rejected_count"],
        "candidates": [
            {
                "candidate_id": candidate_id,
                **summary["by_candidate"][candidate_id],
            }
            for candidate_id in enabled_candidate_ids
        ],
    }


def _candidate_remark_validator(
    *,
    run: Mapping[str, Any],
    catalog: Mapping[str, Any],
    pass_registry: Mapping[str, Any],
):
    enabled_candidate_ids = list(
        run["configuration"].get("enabled_candidate_ids", [])
    )
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    if any(candidate_id not in catalog_ids for candidate_id in enabled_candidate_ids):
        raise ValidationError("candidate raw run enables an unknown candidate")

    def validate(path: Path, case: Mapping[str, Any]) -> None:
        summary = validate_candidate_remark_jsonl(
            path,
            catalog=catalog,
            pass_registry=pass_registry,
            enabled_candidate_ids=enabled_candidate_ids,
            candidate_registry_sha256=sha256_json(catalog),
            pipeline_profile_id=run["provenance"]["pipeline_profile_id"],
            pipeline_profile_sha256=run["provenance"][
                "pipeline_profile_sha256"
            ],
            require_candidate_observation=False,
        )
        expected = _normalized_remark_summary(
            summary, enabled_candidate_ids=enabled_candidate_ids
        )
        if (
            case["remarks_event_count"] != summary["event_count"]
            or case.get("candidate_remark_summary") != expected
        ):
            raise ValidationError(
                f"candidate raw remark summary differs from run case: {case['case_id']}"
            )

    return validate


def _verify_candidate_run_raw_evidence(
    *,
    run_path: Path,
    state_root: Path,
    catalog: Mapping[str, Any],
    pass_registry: Mapping[str, Any],
) -> tuple[dict[str, Any], VerifiedRunRawEvidence]:
    run = _load_version(run_path, "run-record.v1", label="candidate raw run")
    verified = verify_run_raw_evidence(
        run_path,
        state_root,
        remark_validator=_candidate_remark_validator(
            run=run, catalog=catalog, pass_registry=pass_registry
        ),
    )
    if verified.document["run_canonical_sha256"] != sha256_json(run):
        raise ValidationError("candidate raw verifier returned another run identity")
    return run, verified


def build_candidate_raw_evidence_registry(
    *,
    plan_path: Path,
    run_paths: Mapping[str, Path],
    workspace_root: Path,
) -> dict[str, Any]:
    """Replay journals/raw files and bind every supplied campaign run immutably."""

    plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="candidate campaign plan"
    )
    return _build_candidate_raw_evidence_registry_from_plan(
        plan=plan,
        run_paths=run_paths,
        workspace_root=workspace_root,
    )


def _build_candidate_raw_evidence_registry_from_plan(
    *,
    plan: Mapping[str, Any],
    run_paths: Mapping[str, Path],
    workspace_root: Path,
) -> dict[str, Any]:
    if plan["candidate_raw_evidence_schema_sha256"] != schema_sha256(
        "candidate-raw-evidence.v1"
    ):
        raise ValidationError("candidate campaign plan binds a stale raw-evidence schema")
    root = _candidate_workspace_root(workspace_root)
    _, state_root, _ = _workspace_directory_path(
        root,
        root / plan["raw_state_root"],
        label="candidate raw evidence state root",
    )
    catalog = _load_frozen_artifact(
        root,
        plan["artifacts"]["candidate_registry"],
        label="candidate raw evidence registry",
        version="candidate-catalog.v1",
    )
    pass_registry = _load_frozen_artifact(
        root,
        plan["artifacts"]["executable_pass_registry"],
        label="candidate raw evidence PassRegistry",
        version="pass-registry.v2",
    )
    screening = _load_frozen_artifact(
        root,
        plan["artifacts"]["screening"],
        label="candidate raw evidence screening",
        version="candidate-screening.v1",
    )
    base_pass_registry = _load_frozen_artifact(
        root,
        plan["artifacts"]["screening_base_pass_registry"],
        label="candidate raw evidence base PassRegistry",
        version="pass-registry.v2",
    )
    if (
        _load_and_reverify_candidate_screening(
            screening_path=root / plan["artifacts"]["screening"]["path"],
            workspace_root=root,
        )
        != screening
        or
        screening["base_pass_registry"]
        != plan["artifacts"]["screening_base_pass_registry"]
        or _require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=pass_registry,
            workspace_root=root,
        )
        != base_pass_registry
    ):
        raise ValidationError("candidate raw evidence PassRegistry bridge differs")
    allowed_main = {
        task["task_id"]
        for task in plan["tasks"]
        if task["task_type"] == "run"
    }
    unknown = sorted(
        task_id
        for task_id in run_paths
        if task_id not in allowed_main and not task_id.startswith("diagnostic.")
    )
    if unknown:
        raise ConfigurationError(
            "unknown candidate raw run task: " + ", ".join(unknown)
        )
    rows: list[dict[str, Any]] = []
    observed_run_ids: set[str] = set()
    for task_id in sorted(run_paths):
        run_path = run_paths[task_id]
        _, physical, relative = _workspace_regular_path(
            root, run_path, label=f"candidate raw run {task_id}"
        )
        run, verified = _verify_candidate_run_raw_evidence(
            run_path=physical,
            state_root=state_root,
            catalog=catalog,
            pass_registry=pass_registry,
        )
        if run["run_id"] in observed_run_ids:
            raise ValidationError("candidate raw registry contains a repeated run id")
        observed_run_ids.add(run["run_id"])
        rows.append(
            {
                "task_id": task_id,
                "run_record": {
                    "path": relative.as_posix(),
                    "canonical_sha256": sha256_json(run),
                    "physical_sha256": sha256_file(physical),
                },
                "verification": verified.document,
            }
        )
    return validate_document(
        {
            "schema_version": "candidate-raw-evidence.v1",
            "registry_id": (
                f"{plan['campaign_id']}:raw:{sha256_json(rows)[:32]}"
            ),
            "campaign_id": plan["campaign_id"],
            "plan_sha256": sha256_json(plan),
            "raw_state_root": plan["raw_state_root"],
            "runs": rows,
        }
    )


def _load_and_reverify_candidate_raw_evidence_registry(
    *,
    plan: Mapping[str, Any],
    registry_path: Path,
    workspace_root: Path,
    expected_run_paths: Mapping[str, Path] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    root = _candidate_workspace_root(workspace_root)
    registry = _load_version(
        registry_path,
        "candidate-raw-evidence.v1",
        label="candidate raw evidence registry",
    )
    if (
        registry["campaign_id"] != plan["campaign_id"]
        or registry["plan_sha256"] != sha256_json(plan)
        or registry["raw_state_root"] != plan["raw_state_root"]
    ):
        raise ValidationError("candidate raw evidence registry binds another campaign")
    recorded_paths = {
        item["task_id"]: root / item["run_record"]["path"]
        for item in registry["runs"]
    }
    if expected_run_paths is not None:
        normalized_expected = {
            task_id: _workspace_regular_path(
                root, path, label=f"candidate raw run {task_id}"
            )[2].as_posix()
            for task_id, path in expected_run_paths.items()
        }
        normalized_recorded = {
            item["task_id"]: item["run_record"]["path"]
            for item in registry["runs"]
        }
        if normalized_recorded != normalized_expected:
            raise ValidationError(
                "candidate raw evidence registry run-path set differs from status inputs"
            )
    expected = _build_candidate_raw_evidence_registry_from_plan(
        plan=plan,
        run_paths=recorded_paths,
        workspace_root=root,
    )
    if expected != registry:
        raise ValidationError(
            "candidate raw evidence registry differs from replayed journals/raw files"
        )
    _, physical_registry, relative_registry = _workspace_regular_path(
        root, registry_path, label="candidate raw evidence registry"
    )
    return registry, {
        "path": relative_registry.as_posix(),
        "canonical_sha256": sha256_json(registry),
        "physical_sha256": sha256_file(physical_registry),
    }


def _verify_candidate_freeze_inputs(
    freeze: Mapping[str, Any],
    *,
    workspace_root: Path,
    candidate_order: list[str],
) -> dict[str, dict[str, Any]]:
    _, observed_tree = _clean_repository_identity(
        workspace_root, freeze["repository"]["repo_commit"]
    )
    if observed_tree != freeze["repository"]["repo_tree"]:
        raise ValidationError("frozen repository tree differs from the current clean HEAD")
    if _frozen_compiler_artifact(
        workspace_root,
        workspace_root / freeze["repository"]["compiler_artifact"]["path"],
    ) != freeze["repository"]["compiler_artifact"]:
        raise ValidationError("frozen compiler artifact physical identity has drifted")
    snapshots = freeze["snapshots"]
    versions = {
        "candidate_registry": "candidate-catalog.v1",
        "executable_pass_registry": "pass-registry.v2",
        "screening_base_pass_registry": "pass-registry.v2",
        "matrix": "candidate-profile-matrix.v1",
        "screening": "candidate-screening.v1",
        "oracle_capture": "candidate-oracle-capture.v1",
    }
    documents = {
        key: _load_frozen_artifact(
            workspace_root,
            snapshots[key],
            label=f"frozen {key}",
            version=version,
        )
        for key, version in versions.items()
    }
    if (
        _load_and_reverify_candidate_screening(
            screening_path=workspace_root / snapshots["screening"]["path"],
            workspace_root=workspace_root,
        )
        != documents["screening"]
        or _require_executable_registry_bridge(
            screening=documents["screening"],
            catalog=documents["candidate_registry"],
            executable_registry=documents["executable_pass_registry"],
            workspace_root=workspace_root,
        )
        != documents["screening_base_pass_registry"]
    ):
        raise ValidationError("frozen PassRegistry bridge differs")
    for key in ("run_record_schema", "candidate_study_schema"):
        _load_frozen_artifact(
            workspace_root,
            snapshots[key],
            label=f"frozen {key}",
        )
    for suite in freeze["suites"]:
        manifest = _load_frozen_artifact(
            workspace_root,
            suite["manifest"],
            label=f"frozen {suite['data_role']} manifest",
            version="benchmark-manifest.v1",
        )
        require_formal_suite_contract(
            role=suite["data_role"],
            manifest=manifest,
            manifest_path=workspace_root / suite["manifest"]["path"],
        )
        if (
            manifest["suite_id"] != suite["suite_id"]
            or len(manifest["cases"]) != suite["case_count"]
        ):
            raise ValidationError(
                f"frozen {suite['data_role']} suite metadata differs from its manifest"
            )
    profile = _load_frozen_artifact(
        workspace_root,
        freeze["base_pipeline_profile"]["artifact"],
        label="frozen candidate-empty profile",
    )
    validate_pipeline_profile_v2(profile, candidate_order=candidate_order)
    protocol_paths = {
        mode: workspace_root / freeze["measurement_protocols"][mode]["path"]
        for mode in ("standard_proxy", "cache_hotblock")
    }
    for mode, path in protocol_paths.items():
        protocol = freeze["measurement_protocols"][mode]
        _load_frozen_artifact(
            workspace_root,
            {
                "path": protocol["path"],
                "canonical_sha256": protocol["protocol_sha256"],
                "physical_sha256": protocol["physical_sha256"],
            },
            label=f"frozen {mode} protocol",
            version="measurement-protocol.v1",
        )
    toolchain_snapshot = freeze["reference_toolchain"]["snapshot"]
    _load_frozen_artifact(
        workspace_root,
        toolchain_snapshot,
        label="frozen reference toolchain",
    )
    environment = build_campaign_environment_contract(
        measurement_protocol_path=protocol_paths["standard_proxy"],
        hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
        reference_toolchain_path=workspace_root / toolchain_snapshot["path"],
        workspace_root=workspace_root,
    )
    for mode in ("standard_proxy", "cache_hotblock"):
        frozen_contract = dict(freeze["measurement_protocols"][mode])
        frozen_contract.pop("path")
        frozen_contract.pop("physical_sha256")
        if frozen_contract != environment["measurement_protocols"][mode]:
            raise ValidationError(
                f"frozen {mode} normalized protocol contract has drifted"
            )
    frozen_toolchain = dict(freeze["reference_toolchain"])
    frozen_toolchain.pop("snapshot")
    environment_toolchain = dict(environment["reference_toolchain"])
    environment_toolchain.pop("snapshot_sha256")
    if frozen_toolchain != environment_toolchain:
        raise ValidationError("frozen reference toolchain contract has drifted")
    screening = documents["screening"]
    capture = documents["oracle_capture"]
    replayed_capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=workspace_root / snapshots["oracle_capture"]["path"],
        workspace_root=workspace_root,
    )
    if (
        replayed_capture != capture
        or
        screening["oracle_capture_sha256"] != sha256_json(capture)
        or screening["candidate_evidence_sha256"]
        != capture["candidate_evidence_sha256"]
        or snapshots["candidate_evidence_sha256"]
        != capture["candidate_evidence_sha256"]
        or snapshots["screening_spec_sha256"]
        != screening["screening_spec_sha256"]
        or snapshots["oracle_plan_sha256"] != capture["oracle_plan_sha256"]
        or screening["pass_registry_sha256"]
        != snapshots["screening_base_pass_registry"]["canonical_sha256"]
        or screening["base_pass_registry"]
        != snapshots["screening_base_pass_registry"]
    ):
        raise ValidationError(
            "frozen screening/Oracle/evidence/plan identities are inconsistent"
        )
    return documents


def _candidate_map(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["candidate_id"]: item for item in catalog["candidates"]}


def _ordered_candidate_paths(
    catalog: Mapping[str, Any], candidate_paths: Mapping[str, Path]
) -> list[tuple[str, Path]]:
    """Preserve the frozen PassRegistry/catalog implementation order."""

    return [
        (item["candidate_id"], candidate_paths[item["candidate_id"]])
        for item in catalog["candidates"]
        if item["candidate_id"] in candidate_paths
    ]


def _require_screening_capture_binding(
    screening: Mapping[str, Any],
    oracle_capture: Mapping[str, Any],
) -> None:
    if (
        screening["oracle_capture_sha256"] != sha256_json(oracle_capture)
        or screening["candidate_evidence_sha256"]
        != oracle_capture["candidate_evidence_sha256"]
        or screening["oracle_threshold"] != 1.10
        or screening["pass_registry_sha256"]
        != screening["base_pass_registry"]["canonical_sha256"]
    ):
        raise ValidationError(
            "candidate freeze screening/Oracle capture binding differs"
        )


def _require_catalog_registry(
    catalog: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> None:
    if registry["schema_version"] != "pass-registry.v2":
        raise ValidationError("candidate execution requires pass-registry.v2")
    if catalog["pass_registry_sha256"] != sha256_json(registry):
        raise ValidationError("candidate registry does not bind the supplied pass registry")
    registry_candidates = [
        item for item in registry["passes"] if item["lifecycle"] == "candidate"
    ]
    registry_by_id = {item["id"]: item for item in registry_candidates}
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    unknown = sorted(set(catalog_ids) - set(registry_by_id))
    if unknown:
        raise ValidationError(
            "candidate catalog references unknown PassRegistry v2 candidates: "
            + ", ".join(unknown)
        )
    expected_order = [
        item["id"] for item in registry_candidates if item["id"] in set(catalog_ids)
    ]
    if catalog_ids != expected_order:
        raise ValidationError("candidate catalog order differs from PassRegistry v2 candidates()")
    for descriptor in catalog["candidates"]:
        registered = registry_by_id[descriptor["candidate_id"]]
        obligations = [
            item["obligation_id"] for item in descriptor["legality_obligations"]
        ]
        if (
            descriptor["stage"] != registered["stage"]
            or descriptor["display_name"] != registered["display_name"]
            or descriptor["remark_pass_id"] != registered["id"]
            or obligations != registered["legality_obligation_ids"]
        ):
            raise ValidationError(
                f"candidate catalog descriptor differs from PassRegistry v2: {descriptor['candidate_id']}"
            )


def _load_screening_base_pass_registry(
    screening: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    base = _load_frozen_artifact(
        workspace_root,
        screening["base_pass_registry"],
        label="candidate screening base PassRegistry v2",
        version="pass-registry.v2",
    )
    if (
        sha256_json(base) != screening["pass_registry_sha256"]
        or any(item["lifecycle"] == "candidate" for item in base["passes"])
    ):
        raise ValidationError(
            "candidate screening base PassRegistry must be the exact pre-implementation export"
        )
    return base


def _require_executable_registry_bridge(
    *,
    screening: Mapping[str, Any],
    catalog: Mapping[str, Any],
    executable_registry: Mapping[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    """Bind the immutable screening registry to the post-implementation export."""

    base = _load_screening_base_pass_registry(
        screening,
        workspace_root=workspace_root,
    )
    _require_catalog_registry(catalog, executable_registry)
    qualified_rows = [
        item
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    qualified_ids = [
        item["implementation_candidate_id"] for item in qualified_rows
    ]
    if not qualified_ids or any(item is None for item in qualified_ids):
        raise ValidationError(
            "candidate executable registry requires a non-empty qualified implementation set"
        )
    executable_candidates = [
        item for item in executable_registry["passes"]
        if item["lifecycle"] == "candidate"
    ]
    executable_non_candidates = [
        item for item in executable_registry["passes"]
        if item["lifecycle"] != "candidate"
    ]
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    catalog_by_id = {
        item["candidate_id"]: item for item in catalog["candidates"]
    }
    executable_by_id = {item["id"]: item for item in executable_candidates}
    if (
        executable_non_candidates != base["passes"]
        or [item["id"] for item in executable_candidates] != qualified_ids
        or catalog_ids != qualified_ids
        or sha256_json(executable_registry) == sha256_json(base)
    ):
        raise ValidationError(
            "candidate executable PassRegistry must equal the base non-candidate projection "
            "plus exactly the qualified implementations"
        )
    for row in qualified_rows:
        candidate_id = row["implementation_candidate_id"]
        screening_obligations = row["legality_obligation_ids"]
        catalog_obligations = [
            item["obligation_id"]
            for item in catalog_by_id[candidate_id]["legality_obligations"]
        ]
        if (
            screening_obligations != catalog_obligations
            or screening_obligations
            != executable_by_id[candidate_id]["legality_obligation_ids"]
        ):
            raise ValidationError(
                "candidate executable PassRegistry legality obligations differ "
                f"from screening: {candidate_id}"
            )
    return base


def _run_ref(run: Mapping[str, Any]) -> dict[str, str]:
    return {
        "run_id": run["run_id"],
        "run_sha256": sha256_json(run),
        "configuration_sha256": run["configuration_sha256"],
    }


def _raw_run_ref_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: document[key]
        for key in (
            "run_id",
            "run_canonical_sha256",
            "run_physical_sha256",
            "terminal_observed_at",
            "terminal_journal_sha256",
            "terminal_journal_event_count",
            "state_tree_sha256",
            "raw_evidence_sha256",
            "attempt_count",
            "terminal_attempt_count",
        )
    }


def _raw_run_ref(verified: VerifiedRunRawEvidence) -> dict[str, Any]:
    return _raw_run_ref_document(verified.document)


def _raw_verifications_by_run_id(
    registry: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    result = {
        item["verification"]["run_id"]: item["verification"]
        for item in registry["runs"]
    }
    if len(result) != len(registry["runs"]):
        raise ValidationError("candidate raw evidence registry repeats a run id")
    return result


def _binding_record(run: Mapping[str, Any]) -> dict[str, Any]:
    provenance = run["provenance"]
    return {
        "repo_commit": provenance["repo_commit"],
        "repo_dirty": provenance["repo_dirty"],
        "tracked_diff_sha256": provenance["tracked_diff_sha256"],
        "compiler_artifact_sha256": provenance["compiler_artifact_sha256"],
        "measurement_protocol_id": provenance["measurement_protocol_id"],
        "measurement_protocol_sha256": provenance["measurement_protocol_sha256"],
        "pipeline_profile_id": provenance["pipeline_profile_id"],
        "pipeline_profile_sha256": provenance["pipeline_profile_sha256"],
    }


def _require_common_binding(
    baseline: Mapping[str, Any],
    other: Mapping[str, Any],
    *,
    label: str,
) -> None:
    common_keys = (
        "repo_commit",
        "repo_dirty",
        "tracked_diff_sha256",
        "compiler_artifact_sha256",
        "measurement_protocol_id",
        "measurement_protocol_sha256",
    )
    left = _binding_record(baseline)
    right = _binding_record(other)
    if any(left[key] != right[key] for key in common_keys):
        raise ValidationError(
            f"{label} differs from FULL in artifact/commit/protocol provenance"
        )


def _require_candidate_configuration(
    run: Mapping[str, Any],
    *,
    registry_sha256: str,
    pass_registry_sha256: str,
    enabled_candidate_ids: list[str],
    profile: Mapping[str, Any] | None,
    label: str,
) -> None:
    configuration = run["configuration"]
    if configuration.get("candidate_registry_sha256") != registry_sha256:
        raise ValidationError(f"{label} does not bind the exact candidate registry")
    if configuration.get("candidate_pass_registry_sha256") != pass_registry_sha256:
        raise ValidationError(f"{label} does not bind the exact PassRegistry v2")
    if configuration.get("enabled_candidate_ids") != enabled_candidate_ids:
        raise ValidationError(f"{label} has an unexpected enabled candidate set")
    if profile is not None and (
        run["provenance"]["pipeline_profile_id"] != profile["profile_id"]
        or run["provenance"]["pipeline_profile_sha256"] != profile["profile_sha256"]
        or configuration.get("pipeline_profile_file_sha256")
        != profile["profile_sha256"]
    ):
        raise ValidationError(f"{label} does not bind the exact matrix PipelineProfile v2")


def _candidate_profile_bytes(profile: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(profile) + b"\n"


def _workspace_output_directory(
    workspace_root: Path,
    output_directory: Path,
    *,
    label: str,
) -> Path:
    """Create an in-workspace directory without following lexical symlinks."""

    root = _candidate_workspace_root(workspace_root)
    lexical = (
        output_directory
        if output_directory.is_absolute()
        else root / output_directory
    ).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must stay within workspace_root") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValidationError(f"{label} must not traverse a symbolic link")
        if cursor.exists():
            if not cursor.is_dir():
                raise ValidationError(f"{label} must be a directory")
        else:
            cursor.mkdir()
        observed = resolve_without_symlinks(cursor, label=label)
        if observed != cursor:
            raise ValidationError(f"{label} resolves to another path")
    return cursor


def _publish_immutable_json(
    destination: Path,
    document: Mapping[str, Any],
    *,
    label: str,
) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    if destination.is_symlink():
        raise ValidationError(f"{label} must not be a symbolic link")
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != payload:
            raise ValidationError(f"{label} already exists with different bytes")
        return
    durable_create_json(destination, document)


def _verify_matrix_profiles(
    workspace_root: Path,
    matrix: Mapping[str, Any],
    *,
    candidate_order: list[str],
) -> dict[str, Mapping[str, Any]]:
    root = _candidate_workspace_root(workspace_root)
    result: dict[str, Mapping[str, Any]] = {}
    for record in matrix["profiles"]:
        _, path, _ = _workspace_regular_path(
            root,
            Path(record["path"]),
            label=f"candidate profile {record['profile_id']}",
        )
        if sha256_file(path) != record["profile_sha256"]:
            raise ValidationError(
                f"candidate profile physical hash differs: {record['profile_id']}"
            )
        profile = load_pipeline_profile_v2(path, candidate_order=candidate_order)
        expected_arity = {"candidate_empty": 0, "single": 1, "pair": 2}[
            record["kind"]
        ]
        if (
            profile["base"] != "FULL"
            or profile["disable"] != []
            or profile["enable_candidates"] != record["enabled_candidate_ids"]
            or record["candidate_ids"] != record["enabled_candidate_ids"]
            or len(record["candidate_ids"]) != expected_arity
        ):
            raise ValidationError(
                f"candidate profile FULL/disable/enable contract differs: {record['profile_id']}"
            )
        if record["kind"] == "candidate_empty" and record["profile_id"] != "candidate-empty":
            raise ValidationError("candidate-empty profile id is not canonical")
        result[record["profile_id"]] = record
    return result


def generate_candidate_profile_matrix(
    *,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_id: str,
    workspace_root: Path,
    output_directory: Path,
    pairs: tuple[tuple[str, str], ...] = (),
    top_candidates: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build physical candidate-empty/single/explicit-Top3-pair profiles."""

    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    registry_sha256 = sha256_json(registry)
    _require_catalog_registry(catalog, registry)
    root = _candidate_workspace_root(workspace_root)
    output = _workspace_output_directory(
        root,
        output_directory,
        label="candidate profile output",
    )
    candidate_ids = [item["candidate_id"] for item in catalog["candidates"]]
    known = set(candidate_ids)
    if len(top_candidates) > 3 or len(top_candidates) != len(set(top_candidates)):
        raise ConfigurationError("Top candidate selection must contain at most three unique ids")
    unknown_top = sorted(set(top_candidates) - known)
    if unknown_top:
        raise ConfigurationError("unknown Top candidate: " + ", ".join(unknown_top))
    requested_pairs = [*pairs, *combinations(top_candidates, 2)]
    normalized_pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    candidate_order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    for left, right in requested_pairs:
        if left == right:
            raise ConfigurationError("candidate pair ids must be distinct")
        unknown = sorted({left, right} - known)
        if unknown:
            raise ConfigurationError("candidate pair references unknown id: " + ", ".join(unknown))
        pair = tuple(sorted((left, right), key=candidate_order.__getitem__))
        if pair in seen_pairs:
            raise ConfigurationError(f"duplicate candidate pair: {left}+{right}")
        seen_pairs.add(pair)
        normalized_pairs.append(pair)
    pair_candidates = {
        candidate_id for pair in normalized_pairs for candidate_id in pair
    }
    if len(normalized_pairs) > 3 or len(pair_candidates) > 3:
        raise ConfigurationError(
            "candidate diagnostic matrix may contain only the at-most-three Top3 pairs"
        )
    if len(pair_candidates) == 3 and set(normalized_pairs) != set(
        combinations(
            sorted(pair_candidates, key=candidate_order.__getitem__),
            2,
        )
    ):
        raise ConfigurationError(
            "three-candidate diagnostics require all and only the three Top3 pairs"
        )

    generated: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def add_profile(profile_id: str, kind: str, enabled: list[str]) -> None:
        profile = validate_pipeline_profile_v2(
            {
            "schema_version": 2,
            "base": "FULL",
            "disable": [],
            "enable_candidates": enabled,
            },
            candidate_order=candidate_ids,
        )
        payload = _candidate_profile_bytes(profile)
        filename = safe_slug(profile_id) + ".json"
        physical = output / "profiles" / filename
        generated.append(
            (
                {
                    "profile_id": profile_id,
                    "kind": kind,
                    "candidate_ids": enabled,
                    "enabled_candidate_ids": enabled,
                    "profile_sha256": sha256_bytes(payload),
                    "path": physical.relative_to(root).as_posix(),
                },
                profile,
            )
        )

    add_profile("candidate-empty", "candidate_empty", [])
    for candidate_id in candidate_ids:
        add_profile(f"full+{candidate_id}", "single", [candidate_id])
    for left, right in normalized_pairs:
        add_profile(f"full+{left}+{right}", "pair", [left, right])

    profiles = [record for record, _ in generated]
    schedule: list[dict[str, Any]] = []
    for profile in profiles:
        if profile["kind"] == "candidate_empty":
            continue
        schedule.append(
            {
                "kind": profile["kind"],
                "candidate_ids": profile["candidate_ids"],
                "baseline_profile_id": "candidate-empty",
                "candidate_profile_id": profile["profile_id"],
                "comparison_direction": "baseline_over_variant",
            }
        )
    base_profile = {
        "profile_id": "candidate-empty",
        "profile_sha256": profiles[0]["profile_sha256"],
    }
    matrix = validate_document(
        {
            "schema_version": "candidate-profile-matrix.v1",
            "matrix_id": matrix_id,
            "candidate_registry_sha256": sha256_json(catalog),
            "pass_registry_sha256": registry_sha256,
            "base_pipeline_profile": base_profile,
            "profiles": profiles,
            "schedule": schedule,
        }
    )
    expected_files = {
        (root / record["path"]).relative_to(output).as_posix()
        for record in profiles
    } | {"matrix.json"}
    if output.exists():
        existing_files: set[str] = set()
        existing_directories: set[str] = set()
        for path in output.rglob("*"):
            relative = path.relative_to(output).as_posix()
            if path.is_symlink():
                raise ValidationError(
                    "candidate profile directory contains a symbolic link: "
                    + relative
                )
            if path.is_file():
                existing_files.add(relative)
            elif path.is_dir():
                existing_directories.add(relative)
            else:
                raise ValidationError(
                    "candidate profile directory contains a non-regular entry: "
                    + relative
                )
        unexpected_directories = sorted(existing_directories - {"profiles"})
        if unexpected_directories:
            raise ConfigurationError(
                "candidate profile directory contains unmanaged directories: "
                + ", ".join(unexpected_directories[:10])
            )
        if (existing_files or existing_directories) and (
            existing_files != expected_files
            or existing_directories != {"profiles"}
        ):
            missing = sorted(expected_files - existing_files)
            unexpected = sorted(existing_files - expected_files)
            raise ConfigurationError(
                "candidate profile directory is not the exact managed file set: "
                f"missing={missing[:10]}, unexpected={unexpected[:10]}"
            )
    profiles_directory = _workspace_output_directory(
        root,
        output / "profiles",
        label="candidate profile directory",
    )
    assert profiles_directory == output / "profiles"
    for record, profile in generated:
        _publish_immutable_json(
            root / record["path"],
            profile,
            label=f"candidate profile {record['profile_id']}",
        )
    _publish_immutable_json(
        output / "matrix.json",
        matrix,
        label="candidate profile matrix",
    )
    return matrix


def build_candidate_screening(
    *,
    candidate_evidence_path: Path,
    screening_spec_path: Path,
    pass_registry_path: Path,
    oracle_capture_path: Path,
    workspace_root: Path,
    screening_id: str,
) -> dict[str, Any]:
    evidence = _load_version(
        candidate_evidence_path,
        "candidate-evidence.v1",
        label="candidate evidence",
    )
    spec = _load_version(
        screening_spec_path,
        "candidate-screening-spec.v1",
        label="candidate screening spec",
    )
    pass_registry = _load_version(
        pass_registry_path,
        "pass-registry.v2",
        label="candidate screening PassRegistry v2",
    )
    pass_registry_sha256 = sha256_json(pass_registry)
    if any(item["lifecycle"] == "candidate" for item in pass_registry["passes"]):
        raise ValidationError(
            "candidate screening requires the pre-implementation PassRegistry with zero candidates"
        )
    if spec["pass_registry_sha256"] != pass_registry_sha256:
        raise ValidationError("candidate screening spec binds a different PassRegistry v2")
    pass_by_id = {item["id"]: item for item in pass_registry["passes"]}
    evidence_ids = [item["candidate_id"] for item in evidence["candidates"]]
    if len(evidence_ids) != 11:
        raise ValidationError("candidate screening requires the complete eleven-family evidence pool")
    spec_by_id = {item["candidate_id"]: item for item in spec["candidates"]}
    if set(spec_by_id) != set(evidence_ids):
        raise ValidationError("candidate screening spec does not cover the exact evidence pool")
    capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=oracle_capture_path,
        workspace_root=workspace_root,
    )
    if capture["candidate_evidence_sha256"] != sha256_json(evidence):
        raise ValidationError("Oracle capture binds different candidate evidence")
    capture_by_id = {item["candidate_id"]: item for item in capture["candidates"]}
    if set(capture_by_id) != set(evidence_ids):
        raise ValidationError("Oracle capture does not cover all eleven candidates")
    capture_by_family = {
        item["oracle_family_id"]: item for item in capture["candidates"]
    }
    if len(capture_by_family) != len(capture["candidates"]):
        raise ValidationError("Oracle capture repeats an Oracle family")

    results: list[dict[str, Any]] = []
    duplicate_groups: dict[str, list[str]] = {}
    for evidence_item in evidence["candidates"]:
        candidate_id = evidence_item["candidate_id"]
        structural = spec_by_id[candidate_id]
        if (
            structural["oracle_family_id"]
            != capture_by_id[candidate_id]["oracle_family_id"]
            or structural["oracle_family_id"]
            != evidence_item["cleanroom_oracle_family_id"]
        ):
            raise ValidationError(
                f"candidate screening Oracle family identity differs: {candidate_id}"
            )
        implementation_id = structural["implementation_candidate_id"]
        overlap_ids = structural["overlaps_existing_pass_ids"]
        unknown_overlaps = sorted(set(overlap_ids) - set(pass_by_id))
        candidate_overlaps = sorted(
            pass_id
            for pass_id in overlap_ids
            if pass_id in pass_by_id
            and pass_by_id[pass_id]["lifecycle"] == "candidate"
        )
        if unknown_overlaps or candidate_overlaps:
            raise ValidationError(
                f"candidate screening overlap ids must be existing non-candidate passes: {candidate_id}"
            )
        obligations = evidence_item["legality_obligation_ids"]
        if structural["structural_disposition"] == "eligible":
            if implementation_id is None:
                raise ValidationError(
                    f"eligible candidate lacks an implementation pass id: {candidate_id}"
                )
            if not obligations or any(
                not obligation.startswith(f"{implementation_id}.")
                for obligation in obligations
            ):
                raise ValidationError(
                    f"candidate legality obligations are not scoped to {implementation_id}"
                )
            if implementation_id in pass_by_id:
                raise ValidationError(
                    f"future candidate implementation id conflicts with an existing pass: {implementation_id}"
                )
        reasons: list[str] = []
        structural_reason = structural["structural_reason"]
        if structural_reason is not None:
            reasons.append(structural_reason)
        if evidence_item["legality_proof_path"] != "clear":
            reasons.append("unclear_legality")
        if evidence_item["specification_status"] != "clear":
            reasons.append("unclear_specification")
        primary_family = structural["oracle_family_id"]
        eligible_oracle_refs = structural["eligible_oracle_structure_refs"]
        eligible_ref_tuples = [
            (ref["oracle_family_id"], ref["structure_id"])
            for ref in eligible_oracle_refs
        ]
        captured_by_ref = {
            (family_id, captured["structure_id"]): captured
            for family_id, family_capture in capture_by_family.items()
            for captured in family_capture["structures"]
        }
        unknown_eligible = sorted(set(eligible_ref_tuples) - set(captured_by_ref))
        if unknown_eligible:
            raise ValidationError(
                f"candidate screening allows unknown Oracle structures for {candidate_id}: "
                + ", ".join(f"{family}/{structure}" for family, structure in unknown_eligible)
            )
        source_structures = [
            (primary_family, captured)
            for captured in capture_by_id[candidate_id]["structures"]
        ]
        source_structures.extend(
            (family_id, captured_by_ref[(family_id, structure_id)])
            for family_id, structure_id in eligible_ref_tuples
            if family_id != primary_family
        )
        source_refs = [
            (family_id, captured["structure_id"])
            for family_id, captured in source_structures
        ]
        if len(source_refs) != len(set(source_refs)):
            raise ValidationError(
                f"candidate screening repeats an Oracle source structure: {candidate_id}"
            )
        if eligible_ref_tuples != [
            source_ref
            for source_ref in source_refs
            if source_ref in set(eligible_ref_tuples)
        ]:
            raise ValidationError(
                f"candidate screening eligible structures are not an ordered capture subset: {candidate_id}"
            )
        eligible_ref_set = set(eligible_ref_tuples)
        oracle_structures: list[dict[str, Any]] = []
        for source_family, captured in source_structures:
            geometric_mean = captured["geometric_mean_upper_bound"]
            source_ref = (source_family, captured["structure_id"])
            screening_eligible = source_ref in eligible_ref_set
            meets = bool(
                screening_eligible
                and
                captured["eligible_for_ranking"]
                and geometric_mean is not None
                and geometric_mean >= 1.10
            )
            oracle_structures.append(
                {
                    "oracle_family_id": source_family,
                    "structure_id": captured["structure_id"],
                    "sizes": captured["sizes"],
                    "paired_datasets": captured["paired_datasets"],
                    "eligible_for_ranking": captured["eligible_for_ranking"],
                    "ineligibility_reason": captured["ineligibility_reason"],
                    "geometric_mean_speedup": geometric_mean,
                    "eligible_for_candidate_screening": screening_eligible,
                    "meets_threshold": meets,
                }
            )
        qualifying_structures = [
            {
                "oracle_family_id": item["oracle_family_id"],
                "structure_id": item["structure_id"],
            }
            for item in oracle_structures
            if item["meets_threshold"]
        ]
        if not qualifying_structures and eligible_oracle_refs:
            if any(
                item["eligible_for_candidate_screening"]
                and item["eligible_for_ranking"]
                for item in oracle_structures
            ):
                reasons.append("oracle_structure_below_1_10")
            else:
                reasons.append("no_complete_oracle_structure")
        reasons = list(dict.fromkeys(reasons))
        if structural["structural_disposition"] == "rejected":
            qualification = "rejected"
        elif reasons:
            qualification = "blocked"
        else:
            qualification = "qualified"
        duplicate = structural["duplicate_of"]
        if duplicate is not None:
            duplicate_groups.setdefault(duplicate, []).append(candidate_id)
        results.append(
            {
                "candidate_id": candidate_id,
                "oracle_family_id": structural["oracle_family_id"],
                "implementation_candidate_id": structural[
                    "implementation_candidate_id"
                ],
                "family_kind": structural["family_kind"],
                "overlaps_existing_pass_ids": structural[
                    "overlaps_existing_pass_ids"
                ],
                "duplicate_of": duplicate,
                "eligible_oracle_structure_refs": eligible_oracle_refs,
                "oracle_structures": oracle_structures,
                "qualifying_oracle_structure_refs": qualifying_structures,
                "legality_proof_path": evidence_item["legality_proof_path"],
                "legality_obligation_ids": evidence_item[
                    "legality_obligation_ids"
                ],
                "implementation_cost": evidence_item["implementation_cost"],
                "risk": evidence_item["risk"],
                "specification_status": evidence_item["specification_status"],
                "requires_boom_feature": evidence_item["requires_boom_feature"],
                "qualification_status": qualification,
                "rejection_reasons": reasons,
            }
        )
    return validate_document(
        {
            "schema_version": "candidate-screening.v1",
            "screening_id": screening_id,
            "generated_at": capture["generated_at"],
            "candidate_evidence_sha256": sha256_json(evidence),
            "screening_spec_sha256": sha256_json(spec),
            "pass_registry_sha256": pass_registry_sha256,
            "base_pass_registry": _frozen_artifact_digest(
                workspace_root,
                pass_registry_path,
                pass_registry,
                label="candidate screening base PassRegistry v2",
            ),
            "sources": {
                "candidate_evidence": _frozen_artifact_digest(
                    workspace_root,
                    candidate_evidence_path,
                    evidence,
                    label="candidate screening evidence",
                ),
                "screening_spec": _frozen_artifact_digest(
                    workspace_root,
                    screening_spec_path,
                    spec,
                    label="candidate screening spec",
                ),
                "oracle_capture": _frozen_artifact_digest(
                    workspace_root,
                    oracle_capture_path,
                    capture,
                    label="candidate screening Oracle capture",
                ),
            },
            "oracle_threshold": 1.10,
            "oracle_capture_sha256": sha256_json(capture),
            "candidates": results,
            "duplicate_groups": [
                {
                    "canonical_candidate_id": canonical,
                    "duplicate_candidate_ids": sorted(duplicates),
                }
                for canonical, duplicates in sorted(duplicate_groups.items())
            ],
        }
    )


def _load_and_reverify_candidate_screening(
    *,
    screening_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    """Rebuild screening from every frozen source and require exact identity."""

    root = _candidate_workspace_root(workspace_root)
    _, physical, _ = _workspace_regular_path(
        root,
        screening_path,
        label="candidate screening",
    )
    screening = _load_version(
        physical,
        "candidate-screening.v1",
        label="candidate screening",
    )
    sources = screening["sources"]
    expected = build_candidate_screening(
        candidate_evidence_path=root / sources["candidate_evidence"]["path"],
        screening_spec_path=root / sources["screening_spec"]["path"],
        pass_registry_path=root / screening["base_pass_registry"]["path"],
        oracle_capture_path=root / sources["oracle_capture"]["path"],
        workspace_root=root,
        screening_id=screening["screening_id"],
    )
    if expected != screening:
        raise ValidationError(
            "candidate screening differs from exact source/capture replay"
        )
    return screening


def _terminal_candidate_reason(
    *,
    correctness_failures: int,
    excluded_cases: int,
    censored_cases: int,
    comparable_cases: int,
    paired_candidate_count: int,
) -> str | None:
    if correctness_failures:
        return "correctness_failure"
    if excluded_cases:
        return "incomplete_profile"
    if censored_cases:
        return "right_censored"
    if comparable_cases == 0:
        return "no_comparable_cases"
    if paired_candidate_count == 0:
        return "no_candidate_observation"
    return None


def _require_analysis_terminal_run(
    run: Mapping[str, Any], *, label: str
) -> None:
    """Accept every immutable campaign terminal, including interruption evidence."""

    if run["state"] not in {"completed", "failed", "interrupted"}:
        raise ValidationError(f"{label} is not terminal")


def _measured_case_value(
    case: Mapping[str, Any],
    metric_id: str,
) -> float | None:
    for measurement in case["measurements"]:
        if measurement["metric_id"] == metric_id:
            if measurement["availability"] != "measured" or measurement["value"] is None:
                return None
            return float(measurement["value"])
    return None


def build_candidate_study(
    *,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_path: Path,
    workspace_root: Path,
    raw_state_root: Path,
    baseline_path: Path,
    candidate_paths: Mapping[str, Path],
    study_id: str,
    title: str,
    bootstrap_samples: int = 10_000,
    seed: int = 20260809,
    interaction_paths: Mapping[tuple[str, str], Path] | None = None,
) -> dict[str, Any]:
    """Compare FULL to FULL+candidate directly; ratios are never inverted."""

    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    pass_registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    _require_catalog_registry(catalog, pass_registry)
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate profile matrix"
    )
    registry_sha256 = sha256_json(catalog)
    if matrix["candidate_registry_sha256"] != registry_sha256:
        raise ValidationError("candidate matrix does not bind the supplied registry")
    if matrix["pass_registry_sha256"] != catalog["pass_registry_sha256"]:
        raise ValidationError("candidate matrix/pass-registry binding drifted")
    candidates = _candidate_map(catalog)
    profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    scheduled = {
        item["candidate_ids"][0]: item
        for item in matrix["schedule"]
        if item["kind"] == "single"
    }
    unknown = sorted(set(candidate_paths) - set(scheduled))
    if unknown:
        raise ConfigurationError(
            "candidate run is not scheduled by the matrix: " + ", ".join(unknown)
        )

    _, physical_state_root, _ = _workspace_directory_path(
        workspace_root,
        raw_state_root,
        label="candidate study raw evidence state root",
    )
    baseline, baseline_verified = _verify_candidate_run_raw_evidence(
        run_path=baseline_path,
        state_root=physical_state_root,
        catalog=catalog,
        pass_registry=pass_registry,
    )
    if baseline["state"] != "completed":
        raise ValidationError("candidate study requires a completed all-correct FULL run")
    data_roles = {case["data_role"] for case in baseline["cases"]}
    if len(data_roles) != 1 or next(iter(data_roles)) not in {"B2", "B3", "B4", "B5", "B6"}:
        raise ValidationError("candidate study requires one B2-B6 data role")
    data_role = next(iter(data_roles))
    base_profile = profiles["candidate-empty"]
    if (
        baseline["provenance"]["pipeline_profile_id"] != base_profile["profile_id"]
        or baseline["provenance"]["pipeline_profile_sha256"]
        != base_profile["profile_sha256"]
    ):
        raise ValidationError("FULL run does not bind the matrix base pipeline profile")
    _require_candidate_configuration(
        baseline,
        registry_sha256=registry_sha256,
        pass_registry_sha256=catalog["pass_registry_sha256"],
        enabled_candidate_ids=[],
        profile=base_profile,
        label="FULL run",
    )
    _require_formal_measurement(baseline, require_accela_pipeline=True)
    _require_candidate_run_protocol(baseline, data_role=data_role)
    primary = metric_spec(baseline)
    if primary["source"] == "wall_time":
        raise ValidationError("candidate ranking requires a counter/size primary metric")

    terminal_times = [baseline_verified.document["terminal_observed_at"]]
    results: list[dict[str, Any]] = []
    candidate_raw_refs: list[dict[str, Any]] = []
    for candidate_id, path in _ordered_candidate_paths(catalog, candidate_paths):
        descriptor = candidates[candidate_id]
        candidate, candidate_verified = _verify_candidate_run_raw_evidence(
            run_path=path,
            state_root=physical_state_root,
            catalog=catalog,
            pass_registry=pass_registry,
        )
        _require_analysis_terminal_run(
            candidate, label=f"candidate run {candidate_id}"
        )
        terminal_times.append(candidate_verified.document["terminal_observed_at"])
        if (
            candidate["suite_id"] != baseline["suite_id"]
            or candidate["manifest_sha256"] != baseline["manifest_sha256"]
        ):
            raise ValidationError(f"candidate run suite/manifest differs from FULL: {candidate_id}")
        _require_common_binding(baseline, candidate, label=f"candidate {candidate_id}")
        _require_candidate_configuration(
            candidate,
            registry_sha256=registry_sha256,
            pass_registry_sha256=catalog["pass_registry_sha256"],
            enabled_candidate_ids=[candidate_id],
            profile=profiles[scheduled[candidate_id]["candidate_profile_id"]],
            label=f"candidate {candidate_id}",
        )
        _require_formal_measurement(candidate, require_accela_pipeline=True)
        _require_candidate_run_protocol(candidate, data_role=data_role)
        comparison = compare_runs(baseline, candidate)
        candidate_raw_refs.append(
            {"candidate_id": candidate_id, "run": _raw_run_ref(candidate_verified)}
        )

        obligation_ids = [
            item["obligation_id"] for item in descriptor["legality_obligations"]
        ]
        remark_case_count, remark_totals = _candidate_remark_totals(
            candidate, candidate_ids=[candidate_id]
        )
        paired_candidate_count = remark_totals[candidate_id][
            "paired_candidate_count"
        ]
        applied_count = remark_totals[candidate_id]["applied_count"]
        rejected_count = remark_totals[candidate_id]["rejected_count"]

        interval = bootstrap_geometric_mean_ci(
            comparison.pairs,
            samples=bootstrap_samples,
            seed=seed,
        )
        reason = _terminal_candidate_reason(
            correctness_failures=comparison.correctness_failures,
            excluded_cases=comparison.excluded_cases,
            censored_cases=comparison.censored_cases,
            comparable_cases=len(comparison.pairs),
            paired_candidate_count=paired_candidate_count,
        )
        baseline_cases = {case["case_id"]: case for case in baseline["cases"]}
        candidate_cases = {case["case_id"]: case for case in candidate["cases"]}
        static_pairs = [
            (
                _measured_case_value(baseline_cases[pair.case_id], "elf_text_bytes"),
                _measured_case_value(candidate_cases[pair.case_id], "elf_text_bytes"),
            )
            for pair in comparison.pairs
        ]
        if static_pairs and all(left is not None and right is not None for left, right in static_pairs):
            static_full = sum(float(left) for left, _ in static_pairs if left is not None)
            static_candidate = sum(float(right) for _, right in static_pairs if right is not None)
            if static_full <= 0 or static_candidate <= 0:
                static_full = None
                static_candidate = None
                static_ratio = None
            else:
                static_ratio = static_full / static_candidate
        else:
            static_full = None
            static_candidate = None
            static_ratio = None
        results.append(
            {
                "candidate_id": candidate_id,
                "logical_profile_id": scheduled[candidate_id]["candidate_profile_id"],
                "run_id": candidate["run_id"],
                "run_sha256": sha256_json(candidate),
                "configuration_sha256": candidate["configuration_sha256"],
                "enabled_candidate_ids": [candidate_id],
                "comparable_cases": len(comparison.pairs),
                "comparable_source_groups": len(
                    {pair.source_group_id for pair in comparison.pairs}
                ),
                "correctness_failures": comparison.correctness_failures,
                "censored_cases": comparison.censored_cases,
                "excluded_cases": comparison.excluded_cases,
                "eligible_for_ranking": reason is None,
                "ineligibility_reason": reason,
                "case_geometric_mean_speedup": comparison.geometric_mean_speedup,
                "source_group_geometric_mean_speedup": (
                    comparison.source_group_geometric_mean_speedup
                ),
                "confidence_interval_95": (
                    None
                    if interval is None
                    else {"low": interval[0], "high": interval[1]}
                ),
                "static_text_bytes_full": static_full,
                "static_text_bytes_full_plus_candidate": static_candidate,
                "static_text_ratio": static_ratio,
                "remarks": {
                    "case_count": remark_case_count,
                    "paired_candidate_count": paired_candidate_count,
                    "applied_count": applied_count,
                    "rejected_count": rejected_count,
                    "legality_obligation_ids": obligation_ids,
                },
                "per_cases": [
                    {
                        "case_id": pair.case_id,
                        "source_group": pair.source_group_id,
                        "family": pair.family,
                        "target": pair.target,
                        "weight": pair.weight,
                        "metric_full": pair.baseline_value,
                        "metric_full_plus_candidate": pair.candidate_value,
                        "speedup": pair.speedup,
                    }
                    for pair in comparison.pairs
                ],
                "families": family_geometric_means(comparison.pairs),
            }
        )
    requested_interactions = interaction_paths or {}
    interactions: list[dict[str, Any]] = []
    interaction_raw_refs: list[dict[str, Any]] = []
    if requested_interactions:
        if data_role != "B3":
            raise ConfigurationError(
                "candidate pair interactions are diagnostic B3 evidence only"
            )
        candidate_order = {
            candidate_id: index for index, candidate_id in enumerate(candidates)
        }
        normalized_paths: dict[tuple[str, str], Path] = {}
        for raw_pair, path in requested_interactions.items():
            if len(raw_pair) != 2 or raw_pair[0] == raw_pair[1]:
                raise ConfigurationError(
                    "candidate interaction requires two distinct candidate ids"
                )
            unknown_pair = sorted(set(raw_pair) - set(candidates))
            if unknown_pair:
                raise ConfigurationError(
                    "candidate interaction references unknown candidates: "
                    + ", ".join(unknown_pair)
                )
            pair = tuple(sorted(raw_pair, key=candidate_order.__getitem__))
            if pair in normalized_paths:
                raise ConfigurationError(
                    f"duplicate candidate interaction: {pair[0]}+{pair[1]}"
                )
            normalized_paths[pair] = path
        eligible_singles = sorted(
            (item for item in results if item["eligible_for_ranking"]),
            key=lambda item: (
                -float(item["case_geometric_mean_speedup"]),
                item["candidate_id"],
            ),
        )
        top_candidate_ids = [
            item["candidate_id"] for item in eligible_singles[:3]
        ]
        ordered_pairs = [
            tuple(sorted(pair, key=candidate_order.__getitem__))
            for pair in combinations(top_candidate_ids, 2)
        ]
        expected_pairs = set(ordered_pairs)
        if set(normalized_paths) != expected_pairs:
            raise ValidationError(
                "candidate interactions must cover exactly the B3 single-GM Top3 pairs"
            )
        scheduled_pairs = {
            tuple(item["candidate_ids"]): item
            for item in matrix["schedule"]
            if item["kind"] == "pair"
        }
        single_by_id = {item["candidate_id"]: item for item in results}
        for pair in ordered_pairs:
            schedule = scheduled_pairs.get(pair)
            if schedule is None:
                raise ValidationError(
                    f"candidate matrix lacks the selected Top3 pair: {pair[0]}+{pair[1]}"
                )
            pair_key = "+".join(pair)
            run, pair_verified = _verify_candidate_run_raw_evidence(
                run_path=normalized_paths[pair],
                state_root=physical_state_root,
                catalog=catalog,
                pass_registry=pass_registry,
            )
            _require_analysis_terminal_run(
                run, label=f"candidate interaction run {pair_key}"
            )
            terminal_times.append(pair_verified.document["terminal_observed_at"])
            if (
                run["suite_id"] != baseline["suite_id"]
                or run["manifest_sha256"] != baseline["manifest_sha256"]
            ):
                raise ValidationError(
                    f"candidate interaction suite/manifest differs from FULL: {pair_key}"
                )
            _require_common_binding(
                baseline, run, label=f"candidate interaction {pair_key}"
            )
            profile = profiles[schedule["candidate_profile_id"]]
            _require_candidate_configuration(
                run,
                registry_sha256=registry_sha256,
                pass_registry_sha256=catalog["pass_registry_sha256"],
                enabled_candidate_ids=list(pair),
                profile=profile,
                label=f"candidate interaction {pair_key}",
            )
            _require_formal_measurement(run, require_accela_pipeline=True)
            _require_candidate_run_protocol(run, data_role=data_role)
            comparison = compare_runs(baseline, run)
            _, pair_totals = _candidate_remark_totals(
                run, candidate_ids=list(pair)
            )
            observations = {
                candidate_id: pair_totals[candidate_id]["paired_candidate_count"]
                for candidate_id in pair
            }
            interaction_raw_refs.append(
                {
                    "candidate_ids": list(pair),
                    "run": _raw_run_ref(pair_verified),
                }
            )
            reason = _terminal_candidate_reason(
                correctness_failures=comparison.correctness_failures,
                excluded_cases=comparison.excluded_cases,
                censored_cases=comparison.censored_cases,
                comparable_cases=len(comparison.pairs),
                paired_candidate_count=min(observations.values()),
            )
            constituent_results = [single_by_id[candidate_id] for candidate_id in pair]
            if reason is None and any(
                not item["eligible_for_ranking"] for item in constituent_results
            ):
                reason = "constituent_ineligible"
            pair_gm = comparison.geometric_mean_speedup
            expected_speedup = (
                math.prod(
                    float(item["case_geometric_mean_speedup"])
                    for item in constituent_results
                )
                if reason is None
                else None
            )
            interactions.append(
                {
                    "candidate_ids": list(pair),
                    "logical_profile_id": schedule["candidate_profile_id"],
                    "run_id": run["run_id"],
                    "run_sha256": sha256_json(run),
                    "configuration_sha256": run["configuration_sha256"],
                    "enabled_candidate_ids": list(pair),
                    "comparable_cases": len(comparison.pairs),
                    "correctness_failures": comparison.correctness_failures,
                    "censored_cases": comparison.censored_cases,
                    "excluded_cases": comparison.excluded_cases,
                    "candidate_observations": [
                        {
                            "candidate_id": candidate_id,
                            "paired_candidate_count": observations[candidate_id],
                        }
                        for candidate_id in pair
                    ],
                    "eligible_for_ranking": reason is None,
                    "ineligibility_reason": reason,
                    "pair_case_geometric_mean_speedup": (
                        pair_gm if reason is None else None
                    ),
                    "expected_multiplicative_speedup": expected_speedup,
                    "delta_ln_geometric_mean": (
                        math.log(float(pair_gm))
                        - sum(
                            math.log(float(item["case_geometric_mean_speedup"]))
                            for item in constituent_results
                        )
                        if reason is None and pair_gm is not None
                        else None
                    ),
                    "per_cases": [
                        {
                            "case_id": item.case_id,
                            "source_group": item.source_group_id,
                            "family": item.family,
                            "target": item.target,
                            "weight": item.weight,
                            "metric_full": item.baseline_value,
                            "metric_full_plus_candidate": item.candidate_value,
                            "speedup": item.speedup,
                        }
                        for item in comparison.pairs
                    ],
                }
            )

    return validate_document(
        {
            "schema_version": "candidate-study.v1",
            "study_id": study_id,
            "title": title,
            "generated_at": _latest_evidence_timestamp(terminal_times),
            "matrix_sha256": sha256_json(matrix),
            "candidate_registry_sha256": registry_sha256,
            "pass_registry_sha256": catalog["pass_registry_sha256"],
            "suite_id": baseline["suite_id"],
            "data_role": data_role,
            "manifest_sha256": baseline["manifest_sha256"],
            "bindings": _binding_record(baseline),
            "baseline": _run_ref(baseline),
            "raw_evidence": {
                "baseline": _raw_run_ref(baseline_verified),
                "candidates": candidate_raw_refs,
                "interactions": interaction_raw_refs,
            },
            "primary_metric_id": baseline["configuration"]["primary_metric_id"],
            "metric_unit": primary["unit"],
            "bootstrap_samples": bootstrap_samples,
            "seed": seed,
            "candidates": results,
            "interactions": interactions,
        }
    )


def _oracle_pair_rows(
    plan: Mapping[str, Any],
    baseline: Mapping[str, Any],
    optimized: Mapping[str, Any],
) -> list[dict[str, Any]]:
    left_cases = {case["case_id"]: case for case in baseline["cases"]}
    right_cases = {case["case_id"]: case for case in optimized["cases"]}
    primary = baseline["configuration"]["primary_metric_id"]
    rows: list[dict[str, Any]] = []
    for pair in plan["pairs"]:
        identity = pair["pair_id"].split(":")
        if (
            len(identity) != 3
            or identity[0] != pair["family"]
            or identity[2] not in {"small", "medium", "large"}
        ):
            raise ValidationError(
                "candidate Oracle pair_id must be FAMILY:STRUCTURE:small|medium|large"
            )
        _, structure_id, size = identity
        left = left_cases.get(pair["baseline"]["case_id"])
        right = right_cases.get(pair["optimized"]["case_id"])
        reason: str | None = None
        if left is None:
            reason = "baseline_case_missing"
        elif right is None:
            reason = "optimized_case_missing"
        else:
            for case, descriptor, leg in (
                (left, pair["baseline"], "baseline"),
                (right, pair["optimized"], "optimized"),
            ):
                immutable = {
                    "family": pair["family"],
                    "target": pair["target"],
                    "data_role": plan["manifest_data_role"],
                    "source_group": descriptor["source_group"],
                    "source_sha256": descriptor["source_sha256"],
                    "input_sha256": pair["input_sha256"],
                    "expected_output_sha256": pair["expected_output_sha256"],
                }
                if any(case[key] != value for key, value in immutable.items()):
                    raise ValidationError(
                        f"oracle {leg} case content differs from plan: {pair['pair_id']}"
                    )
                pairing = case.get("oracle_pair")
                if (
                    pairing is None
                    or pairing["pair_id"] != pair["pair_id"]
                    or pairing["leg"] != leg
                ):
                    raise ValidationError(
                        f"oracle {leg} pairing differs from plan: {pair['pair_id']}"
                    )
            statuses = {left["status"], right["status"]}
            if "timeout" in statuses:
                reason = "right_censored"
            elif statuses & {"pending", "cancelled"}:
                reason = "incomplete_run"
            elif statuses != {"passed"}:
                reason = "correctness_failure"
        left_value = (
            case_metric(left, primary)
            if left is not None and left["status"] == "passed"
            else None
        )
        right_value = (
            case_metric(right, primary)
            if right is not None and right["status"] == "passed"
            else None
        )
        rows.append(
            {
                "pair_id": pair["pair_id"],
                "family": pair["family"],
                "structure_id": structure_id,
                "size": size,
                "baseline_case_id": pair["baseline"]["case_id"],
                "optimized_case_id": pair["optimized"]["case_id"],
                "eligible_for_ranking": reason is None,
                "ineligibility_reason": reason,
                "baseline_metric_value": left_value,
                "optimized_metric_value": right_value,
                "speedup": (
                    left_value / right_value
                    if reason is None and left_value is not None and right_value is not None
                    else None
                ),
            }
        )
    return rows


def capture_candidate_oracle(
    *,
    candidate_evidence_path: Path,
    oracle_plan_path: Path,
    baseline_path: Path,
    optimized_path: Path,
    state_root: Path,
    workspace_root: Path,
    capture_id: str,
) -> dict[str, Any]:
    evidence = _load_version(
        candidate_evidence_path,
        "candidate-evidence.v1",
        label="candidate evidence",
    )
    plan = _load_version(oracle_plan_path, "oracle-plan.v1", label="oracle plan")
    if plan["evidence_class"] != "cleanroom":
        raise ValidationError(
            "candidate screening Oracle capture requires the single cleanroom 99-pair plan"
        )
    if len(plan["pairs"]) != 99 or len(evidence["candidates"]) != 11:
        raise ValidationError(
            "candidate screening Oracle capture requires 11 families x 3 structures x 3 sizes"
        )
    baseline = _load_version(baseline_path, "run-record.v1", label="oracle baseline run")
    optimized = _load_version(optimized_path, "run-record.v1", label="oracle optimized run")
    root, physical_state_root, relative_state_root = _workspace_directory_path(
        workspace_root,
        state_root,
        label="candidate Oracle raw evidence state root",
    )
    _, physical_baseline, relative_baseline = _workspace_regular_path(
        root, baseline_path, label="candidate Oracle baseline run"
    )
    _, physical_optimized, relative_optimized = _workspace_regular_path(
        root, optimized_path, label="candidate Oracle optimized run"
    )
    baseline_verified = verify_run_raw_evidence(
        physical_baseline, physical_state_root
    )
    optimized_verified = verify_run_raw_evidence(
        physical_optimized, physical_state_root
    )
    if (
        baseline_verified.document["run_canonical_sha256"] != sha256_json(baseline)
        or optimized_verified.document["run_canonical_sha256"]
        != sha256_json(optimized)
    ):
        raise ValidationError("candidate Oracle raw verifier run identity differs")
    if (
        baseline["run_id"] != plan["baseline_run_id"]
        or optimized["run_id"] != plan["optimized_run_id"]
    ):
        raise ValidationError("oracle run identities do not match the plan")
    profile = plan["pipeline_profile"]
    for label, run in (("baseline", baseline), ("optimized", optimized)):
        configuration = run["configuration"]
        if run["state"] not in {"completed", "failed"}:
            raise ValidationError(f"oracle {label} run is not terminal")
        if (
            run["provenance"]["pipeline_profile_id"] != profile["profile_id"]
            or run["provenance"]["pipeline_profile_sha256"]
            != profile["profile_sha256"]
        ):
            raise ValidationError(f"oracle {label} run profile differs from the plan")
        if (
            configuration["timeout_policy"] != "initial"
            or configuration["baseline_timeout_run_sha256"] is not None
            or configuration["baseline_timeout_run_id"] is not None
            or any(case.get("timeout_derivation") is not None for case in run["cases"])
        ):
            raise ValidationError(
                f"oracle {label} run requires exact initial timeout evidence "
                "without baseline derivations"
            )
        _require_formal_measurement(run, require_accela_pipeline=True)
    _require_common_binding(baseline, optimized, label="oracle optimized run")
    if baseline["configuration"] != optimized["configuration"]:
        raise ValidationError("oracle source legs require identical run configuration")
    primary = metric_spec(baseline)
    if primary != metric_spec(optimized):
        raise ValidationError("oracle source legs differ in primary metric")
    rows = _oracle_pair_rows(plan, baseline, optimized)
    results: list[dict[str, Any]] = []
    family_ids = [
        descriptor["cleanroom_oracle_family_id"]
        for descriptor in evidence["candidates"]
    ]
    if any(family_id is None for family_id in family_ids) or len(family_ids) != len(
        set(family_ids)
    ):
        raise ValidationError(
            "all eleven candidate families require distinct cleanroom Oracle family ids"
        )
    if set(family_ids) != {row["family"] for row in rows}:
        raise ValidationError(
            "candidate cleanroom family mapping differs from the 99-pair Oracle plan"
        )
    for descriptor in evidence["candidates"]:
        family_id = descriptor["cleanroom_oracle_family_id"]
        assert family_id is not None
        selected = [
            row for row in rows if row["family"] == family_id
        ]
        structure_ids = sorted({row["structure_id"] for row in selected})
        if len(selected) != 9 or len(structure_ids) != 3:
            raise ValidationError(
                f"candidate Oracle family lacks exactly 3 structures x 3 sizes: {family_id}"
            )
        structures: list[dict[str, Any]] = []
        for structure_id in structure_ids:
            structure_rows = [
                row for row in selected if row["structure_id"] == structure_id
            ]
            by_size = {row["size"]: row for row in structure_rows}
            if len(structure_rows) != 3 or set(by_size) != {
                "small",
                "medium",
                "large",
            }:
                raise ValidationError(
                    f"candidate Oracle structure lacks one pair per size: {family_id}:{structure_id}"
                )
            reason = (
                None
                if all(row["eligible_for_ranking"] for row in structure_rows)
                else "incomplete_or_incorrect_oracle"
            )
            structures.append(
                {
                    "structure_id": structure_id,
                    "sizes": {
                        size: by_size[size]
                        for size in ("small", "medium", "large")
                    },
                    "paired_datasets": 3,
                    "eligible_for_ranking": reason is None,
                    "ineligibility_reason": reason,
                    "geometric_mean_upper_bound": (
                        weighted_geometric_mean(
                            (float(by_size[size]["speedup"]), 1.0)
                            for size in ("small", "medium", "large")
                        )
                        if reason is None
                        else None
                    ),
                }
            )
        results.append(
            {
                "candidate_id": descriptor["candidate_id"],
                "oracle_family_id": family_id,
                "structures": structures,
            }
        )
    return validate_document(
        {
            "schema_version": "candidate-oracle-capture.v1",
            "capture_id": capture_id,
            "generated_at": _latest_evidence_timestamp(
                [
                    baseline_verified.document["terminal_observed_at"],
                    optimized_verified.document["terminal_observed_at"],
                ]
            ),
            "candidate_evidence_sha256": sha256_json(evidence),
            "oracle_plan_sha256": sha256_json(plan),
            "sources": {
                "candidate_evidence": _frozen_artifact_digest(
                    root,
                    candidate_evidence_path,
                    evidence,
                    label="candidate Oracle evidence source",
                ),
                "oracle_plan": _frozen_artifact_digest(
                    root,
                    oracle_plan_path,
                    plan,
                    label="candidate Oracle plan source",
                ),
            },
            "evidence_class": "cleanroom",
            "pair_count": 99,
            "bindings": _binding_record(baseline),
            "baseline": _run_ref(baseline),
            "optimized": _run_ref(optimized),
            "raw_state_root": relative_state_root.as_posix(),
            "raw_evidence": {
                "baseline": {
                    "run_record_path": relative_baseline.as_posix(),
                    **_raw_run_ref(baseline_verified),
                },
                "optimized": {
                    "run_record_path": relative_optimized.as_posix(),
                    **_raw_run_ref(optimized_verified),
                },
            },
            "primary_metric_id": baseline["configuration"]["primary_metric_id"],
            "metric_unit": primary["unit"],
            "candidates": results,
        }
    )


def _load_and_reverify_candidate_oracle_capture(
    *,
    capture_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    root = _candidate_workspace_root(workspace_root)
    capture = _load_version(
        capture_path,
        "candidate-oracle-capture.v1",
        label="candidate Oracle capture",
    )
    evidence_artifact = capture["sources"]["candidate_evidence"]
    plan_artifact = capture["sources"]["oracle_plan"]
    _load_frozen_artifact(
        root,
        evidence_artifact,
        label="candidate Oracle evidence source",
        version="candidate-evidence.v1",
    )
    _load_frozen_artifact(
        root,
        plan_artifact,
        label="candidate Oracle plan source",
        version="oracle-plan.v1",
    )
    expected = capture_candidate_oracle(
        candidate_evidence_path=root / evidence_artifact["path"],
        oracle_plan_path=root / plan_artifact["path"],
        baseline_path=root
        / capture["raw_evidence"]["baseline"]["run_record_path"],
        optimized_path=root
        / capture["raw_evidence"]["optimized"]["run_record_path"],
        state_root=root / capture["raw_state_root"],
        workspace_root=root,
        capture_id=capture["capture_id"],
    )
    if expected != capture:
        raise ValidationError(
            "candidate Oracle capture differs from replayed source-leg raw evidence"
        )
    return capture


def build_candidate_campaign_plan(
    *,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_path: Path,
    screening_path: Path,
    suite_paths: Mapping[str, Path],
    measurement_protocol_path: Path,
    compiler_artifact_path: Path,
    raw_state_root: Path,
    workspace_root: Path,
    campaign_id: str,
) -> dict[str, Any]:
    """Build the single authoritative B1-through-final candidate task DAG."""

    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    pass_registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate profile matrix"
    )
    screening = _load_and_reverify_candidate_screening(
        screening_path=screening_path,
        workspace_root=workspace_root,
    )
    measurement_protocol = _load_version(
        measurement_protocol_path,
        "measurement-protocol.v1",
        label="candidate standard measurement protocol",
    )
    if measurement_protocol["measurement_mode"] != "standard_proxy":
        raise ValidationError("candidate formal DAG requires standard_proxy protocol")
    _require_executable_registry_bridge(
        screening=screening,
        catalog=catalog,
        executable_registry=pass_registry,
        workspace_root=workspace_root,
    )
    registry_sha256 = sha256_json(catalog)
    pass_registry_sha256 = sha256_json(pass_registry)
    if (
        matrix["candidate_registry_sha256"] != registry_sha256
        or matrix["pass_registry_sha256"] != pass_registry_sha256
    ):
        raise ValidationError("candidate campaign matrix/registry binding differs")
    qualified_ids = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    assert all(item is not None for item in qualified_ids)
    candidate_ids = [item["candidate_id"] for item in catalog["candidates"]]
    if candidate_ids != qualified_ids:
        raise ValidationError(
            "candidate campaign registry must contain exactly the qualified screening candidates"
        )
    profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=candidate_ids,
    )
    if any(
        item["kind"] == "pair"
        for item in [*matrix["profiles"], *matrix["schedule"]]
    ):
        raise ValidationError(
            "pre-B3 formal candidate matrix must be singles-only; pairs belong to the B3-bound diagnostic child plan"
        )
    single_profiles = {
        item["candidate_ids"][0]: profiles[item["candidate_profile_id"]]
        for item in matrix["schedule"]
        if item["kind"] == "single"
    }
    if set(single_profiles) != set(candidate_ids):
        raise ValidationError("candidate campaign matrix lacks an exact single profile set")
    if set(suite_paths) != set(_CANDIDATE_SUITE_CASE_COUNTS):
        raise ConfigurationError("candidate campaign plan requires exactly B1 through B6 manifests")
    suites: list[dict[str, Any]] = []
    for role, case_count in _CANDIDATE_SUITE_CASE_COUNTS.items():
        manifest = _load_version(
            suite_paths[role],
            "benchmark-manifest.v1",
            label=f"candidate {role} manifest",
        )
        require_formal_suite_contract(
            role=role, manifest=manifest, manifest_path=suite_paths[role]
        )
        suites.append(
            {
                "data_role": role,
                "suite_id": manifest["suite_id"],
                "manifest": _frozen_artifact_digest(
                    workspace_root,
                    suite_paths[role],
                    manifest,
                    label=f"candidate {role} manifest",
                ),
                "case_count": case_count,
            }
        )

    tasks: list[dict[str, Any]] = []

    def add_task(
        *,
        task_id: str,
        task_type: str,
        stage: str,
        kind: str,
        candidate_ids_for_task: list[str],
        data_role: str | None,
        dependencies: list[str],
        terminal_dependencies: list[str],
        gate_kind: str = "always",
        gate_candidate_id: str | None = None,
        profile: Mapping[str, Any] | None = None,
        reference_profile_id: str | None = None,
        ranking_evidence: bool = False,
    ) -> None:
        tasks.append(
            {
                "task_id": task_id,
                "task_type": task_type,
                "stage": stage,
                "kind": kind,
                "candidate_ids": candidate_ids_for_task,
                "data_role": data_role,
                "run_id": (
                    f"{campaign_id}:{task_id}" if task_type == "run" else None
                ),
                "logical_profile_id": (
                    profile["profile_id"] if profile is not None else None
                ),
                "candidate_profile_sha256": (
                    profile["profile_sha256"] if profile is not None else None
                ),
                "candidate_profile_path": (
                    profile["path"] if profile is not None else None
                ),
                "reference_profile_id": reference_profile_id,
                "dependencies": dependencies,
                "terminal_dependencies": terminal_dependencies,
                "gate": {
                    "kind": gate_kind,
                    "candidate_id": gate_candidate_id,
                },
                "ranking_evidence": ranking_evidence,
            }
        )

    baseline_profile = profiles["candidate-empty"]
    add_task(
        task_id="run.B1.full",
        task_type="run",
        stage="B1",
        kind="candidate_empty",
        candidate_ids_for_task=[],
        data_role="B1",
        dependencies=[],
        terminal_dependencies=[],
        profile=baseline_profile,
    )
    previous_b1: str | None = None
    for candidate_id in candidate_ids:
        task_id = f"run.B1.{candidate_id}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B1",
            kind="single",
            candidate_ids_for_task=[candidate_id],
            data_role="B1",
            dependencies=["run.B1.full"],
            terminal_dependencies=([] if previous_b1 is None else [previous_b1]),
            profile=single_profiles[candidate_id],
        )
        previous_b1 = task_id
    b1_tasks = [f"run.B1.{item}" for item in candidate_ids]
    add_task(
        task_id="run.B2.full",
        task_type="run",
        stage="B2",
        kind="candidate_empty",
        candidate_ids_for_task=[],
        data_role="B2",
        dependencies=[],
        terminal_dependencies=b1_tasks,
        profile=baseline_profile,
    )
    previous_b2: str | None = None
    for candidate_id in candidate_ids:
        task_id = f"run.B2.{candidate_id}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B2",
            kind="single",
            candidate_ids_for_task=[candidate_id],
            data_role="B2",
            dependencies=["run.B2.full"],
            terminal_dependencies=[
                f"run.B1.{candidate_id}",
                *([] if previous_b2 is None else [previous_b2]),
            ],
            gate_kind="b1_completed",
            gate_candidate_id=candidate_id,
            profile=single_profiles[candidate_id],
        )
        previous_b2 = task_id
    add_task(
        task_id="study.B2",
        task_type="study",
        stage="B2",
        kind="study",
        candidate_ids_for_task=[],
        data_role="B2",
        dependencies=["run.B2.full"],
        terminal_dependencies=[f"run.B2.{item}" for item in candidate_ids],
    )
    add_task(
        task_id="freeze",
        task_type="freeze",
        stage="freeze",
        kind="freeze",
        candidate_ids_for_task=[],
        data_role=None,
        dependencies=["study.B2"],
        terminal_dependencies=[],
        gate_kind="b2_formal_complete",
    )
    add_task(
        task_id="run.B3.full",
        task_type="run",
        stage="B3",
        kind="candidate_empty",
        candidate_ids_for_task=[],
        data_role="B3",
        dependencies=["freeze"],
        terminal_dependencies=[],
        profile=baseline_profile,
        ranking_evidence=True,
    )
    previous_b3_profile = "run.B3.full"
    for key, reference_profile_id in (
        ("gcc", "gcc-13.3-o2"),
        ("clang", "clang-18-o3"),
    ):
        task_id = f"run.B3.{key}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B3",
            kind="reference",
            candidate_ids_for_task=[],
            data_role="B3",
            dependencies=[
                "freeze", *(["run.B3.full"] if key == "gcc" else [])
            ],
            terminal_dependencies=(
                [] if key == "gcc" else [previous_b3_profile]
            ),
            reference_profile_id=reference_profile_id,
            ranking_evidence=False,
        )
        previous_b3_profile = task_id
    for candidate_id in candidate_ids:
        task_id = f"run.B3.{candidate_id}"
        add_task(
            task_id=task_id,
            task_type="run",
            stage="B3",
            kind="single",
            candidate_ids_for_task=[candidate_id],
            data_role="B3",
            dependencies=["freeze", "run.B3.full"],
            terminal_dependencies=[
                f"run.B1.{candidate_id}", previous_b3_profile
            ],
            gate_kind="b1_completed",
            gate_candidate_id=candidate_id,
            profile=single_profiles[candidate_id],
            ranking_evidence=True,
        )
        previous_b3_profile = task_id
    add_task(
        task_id="study.B3",
        task_type="study",
        stage="B3",
        kind="study",
        candidate_ids_for_task=[],
        data_role="B3",
        dependencies=["run.B3.full"],
        terminal_dependencies=[
            "run.B3.gcc",
            "run.B3.clang",
            *[f"run.B3.{item}" for item in candidate_ids],
        ],
    )
    previous_validation_study = "study.B3"
    for role in ("B4", "B5", "B6"):
        add_task(
            task_id=f"run.{role}.full",
            task_type="run",
            stage=role,
            kind="candidate_empty",
            candidate_ids_for_task=[],
            data_role=role,
            dependencies=[previous_validation_study],
            terminal_dependencies=[],
            gate_kind="b3_has_promoted",
            profile=baseline_profile,
            ranking_evidence=True,
        )
        previous_profile = f"run.{role}.full"
        for candidate_id in candidate_ids:
            task_id = f"run.{role}.{candidate_id}"
            add_task(
                task_id=task_id,
                task_type="run",
                stage=role,
                kind="single",
                candidate_ids_for_task=[candidate_id],
                data_role=role,
                dependencies=["study.B3", f"run.{role}.full"],
                terminal_dependencies=[previous_profile],
                gate_kind="b3_promoted",
                gate_candidate_id=candidate_id,
                profile=single_profiles[candidate_id],
                ranking_evidence=True,
            )
            previous_profile = task_id
        add_task(
            task_id=f"study.{role}",
            task_type="study",
            stage=role,
            kind="study",
            candidate_ids_for_task=[],
            data_role=role,
            dependencies=[f"run.{role}.full"],
            terminal_dependencies=[f"run.{role}.{item}" for item in candidate_ids],
            gate_kind="b3_has_promoted",
        )
        previous_validation_study = f"study.{role}"
    add_task(
        task_id="final",
        task_type="final",
        stage="final",
        kind="final",
        candidate_ids_for_task=[],
        data_role=None,
        dependencies=["study.B3"],
        terminal_dependencies=["study.B4", "study.B5", "study.B6"],
    )
    root = _candidate_workspace_root(workspace_root)
    _, _, raw_state_relative = _workspace_directory_path(
        root, raw_state_root, label="candidate raw evidence state root"
    )
    base_profile_path = root / baseline_profile["path"]
    repo_commit, repo_tree = _clean_repository_identity(root)
    compiler_artifact = _frozen_compiler_artifact(
        root, compiler_artifact_path
    )
    protocol_artifact = _frozen_artifact_digest(
        root,
        measurement_protocol_path,
        measurement_protocol,
        label="candidate standard measurement protocol",
    )
    executable_pass_registry_artifact = _frozen_artifact_digest(
        workspace_root,
        pass_registry_path,
        pass_registry,
        label="candidate executable PassRegistry v2",
    )
    if (
        executable_pass_registry_artifact["path"]
        == screening["base_pass_registry"]["path"]
    ):
        raise ValidationError(
            "candidate executable PassRegistry must use a new artifact path"
        )
    return validate_document(
        {
            "schema_version": "candidate-campaign-plan.v1",
            "campaign_id": campaign_id,
            "run_namespace": f"{campaign_id}:",
            "repository": {
                "repo_commit": repo_commit,
                "repo_tree": repo_tree,
                "compiler_artifact": compiler_artifact,
            },
            "measurement_protocol": {
                "protocol_id": measurement_protocol["protocol_id"],
                "protocol_sha256": sha256_json(measurement_protocol),
                "artifact": protocol_artifact,
            },
            "artifacts": {
                "candidate_registry": _frozen_artifact_digest(
                    workspace_root, catalog_path, catalog, label="candidate registry"
                ),
                "executable_pass_registry": executable_pass_registry_artifact,
                "screening_base_pass_registry": screening[
                    "base_pass_registry"
                ],
                "matrix": _frozen_artifact_digest(
                    workspace_root, matrix_path, matrix, label="candidate matrix"
                ),
                "screening": _frozen_artifact_digest(
                    workspace_root,
                    screening_path,
                    screening,
                    label="candidate screening",
                ),
            },
            "run_record_schema_sha256": schema_sha256("run-record.v1"),
            "candidate_study_schema_sha256": schema_sha256("candidate-study.v1"),
            "candidate_freeze_schema_sha256": schema_sha256("candidate-freeze.v1"),
            "candidate_raw_evidence_schema_sha256": schema_sha256(
                "candidate-raw-evidence.v1"
            ),
            "raw_state_root": raw_state_relative.as_posix(),
            "base_pipeline_profile": {
                **matrix["base_pipeline_profile"],
                "path": baseline_profile["path"],
                "physical_sha256": sha256_file(base_profile_path),
            },
            "suites": suites,
            "study_ids": {
                role: f"{campaign_id}:study:{role}"
                for role in ("B2", "B3", "B4", "B5", "B6")
            },
            "qualified_candidate_ids": candidate_ids,
            "tasks": tasks,
        }
    )


def _validate_campaign_run(
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    profiles: Mapping[str, Mapping[str, Any]],
) -> None:
    if run["run_id"] != task["run_id"]:
        raise ValidationError(f"campaign task run id differs: {task['task_id']}")
    suite = next(
        item for item in plan["suites"] if item["data_role"] == task["data_role"]
    )
    if (
        run["suite_id"] != suite["suite_id"]
        or run["manifest_sha256"] != suite["manifest"]["canonical_sha256"]
    ):
        raise ValidationError(f"campaign task suite/manifest differs: {task['task_id']}")
    _require_candidate_run_protocol(run, data_role=task["data_role"])
    expected_evidence = "qemu_correctness" if task["data_role"] == "B1" else "qemu_proxy"
    if run["configuration"]["evidence_level"] != expected_evidence:
        raise ValidationError(
            f"campaign task evidence level differs: {task['task_id']}"
        )
    if task["data_role"] == "B1":
        _require_candidate_correctness_run(
            run, label=f"campaign B1 task {task['task_id']}"
        )
    if task["kind"] == "reference":
        if (
            run["provenance"]["pipeline_profile_id"]
            != task["reference_profile_id"]
            or run["configuration"].get("enabled_candidate_ids", [])
        ):
            raise ValidationError(
                f"campaign reference task profile differs: {task['task_id']}"
            )
        return
    _require_candidate_configuration(
        run,
        registry_sha256=plan["artifacts"]["candidate_registry"]["canonical_sha256"],
        pass_registry_sha256=plan["artifacts"]["executable_pass_registry"]["canonical_sha256"],
        enabled_candidate_ids=task["candidate_ids"],
        profile=profiles[task["logical_profile_id"]],
        label=f"campaign task {task['task_id']}",
    )


def _require_frozen_reference_run(
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> None:
    reference = next(
        (
            item
            for item in freeze["reference_toolchain"]["baselines"]
            if item["profile_id"] == task["reference_profile_id"]
        ),
        None,
    )
    if reference is None:
        raise ValidationError("candidate reference run lacks a frozen toolchain baseline")
    compiler = run["configuration"]["compiler"]
    versions = {
        item["tool"]: item for item in run["configuration"]["tool_versions"]
    }
    observed = versions.get(reference["tool"])
    if (
        compiler["kind"] != "external"
        or compiler["executable"] != reference["compiler_executable"]
        or compiler["command_sha256"] != reference["compiler_command_sha256"]
        or run["provenance"]["compiler_artifact_sha256"]
        != freeze["reference_toolchain"]["snapshot"]["physical_sha256"]
        or observed is None
        or observed["actual"] != reference["version"]
        or observed["official_expected"] != reference["version"]
        or observed["comparison"] != "exact"
    ):
        raise ValidationError(
            f"candidate reference run differs from frozen {reference['compiler_baseline']} contract"
        )


def _validate_candidate_diagnostic_run(
    *,
    task: Mapping[str, Any],
    run: Mapping[str, Any],
    profile: Mapping[str, Any],
    candidate_registry_sha256: str,
    pass_registry_sha256: str,
    b3_suite: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> None:
    protocol_contract = freeze["measurement_protocols"][task["measurement_mode"]]
    if (
        run["run_id"] != task["run_id"]
        or run["suite_id"] != b3_suite["suite_id"]
        or run["manifest_sha256"]
        != b3_suite["manifest"]["canonical_sha256"]
        or run["configuration"]["evidence_level"] != "qemu_proxy"
        or run["provenance"]["repo_commit"]
        != freeze["repository"]["repo_commit"]
        or run["provenance"]["repo_dirty"]
        or run["provenance"]["tracked_diff_sha256"] is not None
        or run["provenance"]["compiler_artifact_sha256"]
        != freeze["repository"]["compiler_artifact"]["physical_sha256"]
        or run["provenance"]["measurement_protocol_id"]
        != protocol_contract["protocol_id"]
        or run["provenance"]["measurement_protocol_sha256"]
        != protocol_contract["protocol_sha256"]
    ):
        raise ValidationError(
            f"candidate diagnostic run binding differs: {task['task_id']}"
        )
    _require_candidate_run_protocol(run, data_role="B3")
    _require_candidate_configuration(
        run,
        registry_sha256=candidate_registry_sha256,
        pass_registry_sha256=pass_registry_sha256,
        enabled_candidate_ids=task["candidate_ids"],
        profile=profile,
        label=f"candidate diagnostic run {task['task_id']}",
    )


def _candidate_remark_totals(
    run: Mapping[str, Any],
    *,
    candidate_ids: list[str],
) -> tuple[int, dict[str, dict[str, int]]]:
    totals = {
        candidate_id: {
            "paired_candidate_count": 0,
            "applied_count": 0,
            "rejected_count": 0,
        }
        for candidate_id in candidate_ids
    }
    case_count = 0
    for case in run["cases"]:
        summary = case.get("candidate_remark_summary")
        if summary is None:
            continue
        case_count += 1
        by_id = {
            item["candidate_id"]: item for item in summary["candidates"]
        }
        if set(by_id) != set(candidate_ids):
            raise ValidationError(
                f"candidate raw remark summary enablement differs: {case['case_id']}"
            )
        for candidate_id in candidate_ids:
            for field in (
                "paired_candidate_count",
                "applied_count",
                "rejected_count",
            ):
                totals[candidate_id][field] += by_id[candidate_id][field]
    return case_count, totals


def _candidate_static_text_summary(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    comparison: Any,
) -> tuple[float | None, float | None, float | None]:
    baseline_cases = {case["case_id"]: case for case in baseline["cases"]}
    candidate_cases = {case["case_id"]: case for case in candidate["cases"]}
    pairs = [
        (
            _measured_case_value(baseline_cases[item.case_id], "elf_text_bytes"),
            _measured_case_value(candidate_cases[item.case_id], "elf_text_bytes"),
        )
        for item in comparison.pairs
    ]
    if not pairs or not all(
        left is not None and right is not None for left, right in pairs
    ):
        return None, None, None
    full = sum(float(left) for left, _ in pairs if left is not None)
    variant = sum(float(right) for _, right in pairs if right is not None)
    if full <= 0 or variant <= 0:
        return None, None, None
    return full, variant, full / variant


def _candidate_per_case_rows(comparison: Any) -> list[dict[str, Any]]:
    return [
        {
            "case_id": item.case_id,
            "source_group": item.source_group_id,
            "family": item.family,
            "target": item.target,
            "weight": item.weight,
            "metric_full": item.baseline_value,
            "metric_full_plus_candidate": item.candidate_value,
            "speedup": item.speedup,
        }
        for item in comparison.pairs
    ]


def _validate_study_against_raw_runs(
    *,
    study: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate_runs: Mapping[str, Mapping[str, Any]],
    interaction_runs: Mapping[
        frozenset[str], tuple[Mapping[str, Any], Mapping[str, Any]]
    ],
    catalog: Mapping[str, Any],
    raw_verifications: Mapping[str, Mapping[str, Any]],
) -> None:
    if study["baseline"] != _run_ref(baseline):
        raise ValidationError("candidate study baseline differs from its raw FULL run")
    descriptor_by_id = _candidate_map(catalog)
    result_by_id = {item["candidate_id"]: item for item in study["candidates"]}
    if set(result_by_id) != set(candidate_runs):
        raise ValidationError("candidate study singles differ from raw candidate runs")
    try:
        expected_raw_evidence = {
            "baseline": _raw_run_ref_document(
                raw_verifications[baseline["run_id"]]
            ),
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "run": _raw_run_ref_document(
                        raw_verifications[candidate_runs[candidate_id]["run_id"]]
                    ),
                }
                for candidate_id in result_by_id
            ],
            "interactions": [
                {
                    "candidate_ids": list(task["candidate_ids"]),
                    "run": _raw_run_ref_document(
                        raw_verifications[run["run_id"]]
                    ),
                }
                for task, run in interaction_runs.values()
            ],
        }
    except KeyError as exc:
        raise ValidationError(
            "candidate study raw run is absent from the verified registry"
        ) from exc
    if study["raw_evidence"] != expected_raw_evidence:
        raise ValidationError(
            "candidate study raw-evidence refs differ from replayed journals/raw files"
        )
    for candidate_id, run in candidate_runs.items():
        result = result_by_id[candidate_id]
        comparison = compare_runs(baseline, run)
        interval = bootstrap_geometric_mean_ci(
            comparison.pairs,
            samples=study["bootstrap_samples"],
            seed=study["seed"],
        )
        remark_case_count, totals = _candidate_remark_totals(
            run, candidate_ids=[candidate_id]
        )
        candidate_totals = totals[candidate_id]
        reason = _terminal_candidate_reason(
            correctness_failures=comparison.correctness_failures,
            excluded_cases=comparison.excluded_cases,
            censored_cases=comparison.censored_cases,
            comparable_cases=len(comparison.pairs),
            paired_candidate_count=candidate_totals["paired_candidate_count"],
        )
        static_full, static_variant, static_ratio = _candidate_static_text_summary(
            baseline, run, comparison
        )
        expected = {
            "candidate_id": candidate_id,
            "logical_profile_id": run["provenance"]["pipeline_profile_id"],
            "run_id": run["run_id"],
            "run_sha256": sha256_json(run),
            "configuration_sha256": run["configuration_sha256"],
            "enabled_candidate_ids": [candidate_id],
            "comparable_cases": len(comparison.pairs),
            "comparable_source_groups": len(
                {item.source_group_id for item in comparison.pairs}
            ),
            "correctness_failures": comparison.correctness_failures,
            "censored_cases": comparison.censored_cases,
            "excluded_cases": comparison.excluded_cases,
            "eligible_for_ranking": reason is None,
            "ineligibility_reason": reason,
            "case_geometric_mean_speedup": comparison.geometric_mean_speedup,
            "source_group_geometric_mean_speedup": (
                comparison.source_group_geometric_mean_speedup
            ),
            "confidence_interval_95": (
                None if interval is None else {"low": interval[0], "high": interval[1]}
            ),
            "static_text_bytes_full": static_full,
            "static_text_bytes_full_plus_candidate": static_variant,
            "static_text_ratio": static_ratio,
            "remarks": {
                "case_count": remark_case_count,
                **candidate_totals,
                "legality_obligation_ids": [
                    item["obligation_id"]
                    for item in descriptor_by_id[candidate_id][
                        "legality_obligations"
                    ]
                ],
            },
            "per_cases": _candidate_per_case_rows(comparison),
            "families": family_geometric_means(comparison.pairs),
        }
        if result != expected:
            raise ValidationError(
                f"candidate study metrics differ from raw run: {candidate_id}"
            )
    interaction_by_pair = {
        frozenset(item["candidate_ids"]): item for item in study["interactions"]
    }
    if set(interaction_by_pair) != set(interaction_runs):
        raise ValidationError("candidate study interactions differ from raw pair runs")
    for pair, (task, run) in interaction_runs.items():
        interaction = interaction_by_pair[pair]
        candidate_ids = task["candidate_ids"]
        comparison = compare_runs(baseline, run)
        _, totals = _candidate_remark_totals(run, candidate_ids=candidate_ids)
        observations = [
            {
                "candidate_id": candidate_id,
                "paired_candidate_count": totals[candidate_id][
                    "paired_candidate_count"
                ],
            }
            for candidate_id in candidate_ids
        ]
        reason = _terminal_candidate_reason(
            correctness_failures=comparison.correctness_failures,
            excluded_cases=comparison.excluded_cases,
            censored_cases=comparison.censored_cases,
            comparable_cases=len(comparison.pairs),
            paired_candidate_count=min(
                item["paired_candidate_count"] for item in observations
            ),
        )
        constituent_results = [result_by_id[item] for item in candidate_ids]
        if reason is None and any(
            not item["eligible_for_ranking"] for item in constituent_results
        ):
            reason = "constituent_ineligible"
        pair_gm = comparison.geometric_mean_speedup
        expected_multiplicative = (
            math.prod(
                float(item["case_geometric_mean_speedup"])
                for item in constituent_results
            )
            if reason is None
            else None
        )
        expected = {
            "candidate_ids": candidate_ids,
            "logical_profile_id": task["logical_profile_id"],
            "run_id": run["run_id"],
            "run_sha256": sha256_json(run),
            "configuration_sha256": run["configuration_sha256"],
            "enabled_candidate_ids": candidate_ids,
            "comparable_cases": len(comparison.pairs),
            "correctness_failures": comparison.correctness_failures,
            "censored_cases": comparison.censored_cases,
            "excluded_cases": comparison.excluded_cases,
            "candidate_observations": observations,
            "eligible_for_ranking": reason is None,
            "ineligibility_reason": reason,
            "pair_case_geometric_mean_speedup": (
                pair_gm if reason is None else None
            ),
            "expected_multiplicative_speedup": expected_multiplicative,
            "delta_ln_geometric_mean": (
                math.log(float(pair_gm))
                - sum(
                    math.log(float(item["case_geometric_mean_speedup"]))
                    for item in constituent_results
                )
                if reason is None and pair_gm is not None
                else None
            ),
            "per_cases": _candidate_per_case_rows(comparison),
        }
        if interaction != expected:
            raise ValidationError(
                "candidate interaction metrics differ from raw pair run"
            )


def _validate_candidate_diagnostic_study(
    *,
    study: Mapping[str, Any],
    formal_b3_study: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate_runs: Mapping[str, Mapping[str, Any]],
    pair_runs: Mapping[frozenset[str], tuple[Mapping[str, Any], Mapping[str, Any]]],
    top3_candidate_ids: list[str],
    matrix_sha256: str,
    catalog: Mapping[str, Any],
    raw_verifications: Mapping[str, Mapping[str, Any]],
) -> None:
    formal_by_id = {
        item["candidate_id"]: item for item in formal_b3_study["candidates"]
    }
    if (
        study["data_role"] != "B3"
        or study["matrix_sha256"] != matrix_sha256
        or study["candidate_registry_sha256"]
        != formal_b3_study["candidate_registry_sha256"]
        or study["pass_registry_sha256"]
        != formal_b3_study["pass_registry_sha256"]
        or study["suite_id"] != formal_b3_study["suite_id"]
        or study["manifest_sha256"] != formal_b3_study["manifest_sha256"]
        or study["bindings"] != formal_b3_study["bindings"]
        or study["baseline"] != _run_ref(baseline)
        or study["primary_metric_id"] != formal_b3_study["primary_metric_id"]
        or study["metric_unit"] != formal_b3_study["metric_unit"]
        or study["bootstrap_samples"] != formal_b3_study["bootstrap_samples"]
        or study["seed"] != formal_b3_study["seed"]
    ):
        raise ValidationError("candidate diagnostic study differs from exact formal B3 inputs")
    diagnostic_by_id = {item["candidate_id"]: item for item in study["candidates"]}
    formal_order = [
        item["candidate_id"]
        for item in formal_b3_study["candidates"]
        if item["candidate_id"] in set(top3_candidate_ids)
    ]
    if (
        [item["candidate_id"] for item in study["candidates"]] != formal_order
        or set(candidate_runs) != set(top3_candidate_ids)
        or any(
            diagnostic_by_id[candidate_id] != formal_by_id[candidate_id]
            for candidate_id in top3_candidate_ids
        )
    ):
        raise ValidationError(
            "candidate diagnostic study singles differ from exact formal B3 Top3"
        )
    interaction_by_pair = {
        frozenset(item["candidate_ids"]): item for item in study["interactions"]
    }
    expected_pairs = {
        frozenset(pair) for pair in combinations(top3_candidate_ids, 2)
    }
    if set(interaction_by_pair) != expected_pairs or set(pair_runs) != expected_pairs:
        raise ValidationError("candidate diagnostic study differs from exact Top3 pairs")
    _validate_study_against_raw_runs(
        study=study,
        baseline=baseline,
        candidate_runs=candidate_runs,
        interaction_runs=pair_runs,
        catalog=catalog,
        raw_verifications=raw_verifications,
    )


def _candidate_final_study_ref(
    study: Mapping[str, Any],
    *,
    study_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    artifact = _frozen_artifact_digest(
        workspace_root,
        study_path,
        study,
        label=f"candidate final study {study['data_role']}",
    )
    return {
        "study_id": study["study_id"],
        "path": artifact["path"],
        "study_sha256": artifact["canonical_sha256"],
        "physical_sha256": artifact["physical_sha256"],
        "suite_id": study["suite_id"],
        "manifest_sha256": study["manifest_sha256"],
        "candidate_count": len(study["candidates"]),
        "baseline_run_id": study["baseline"]["run_id"],
        "baseline_run_sha256": study["baseline"]["run_sha256"],
        "baseline_configuration_sha256": study["baseline"][
            "configuration_sha256"
        ],
    }


def _candidate_reference_toolchain_context(
    freeze: Mapping[str, Any],
) -> dict[str, Any]:
    toolchain = freeze["reference_toolchain"]
    return {
        "snapshot": toolchain["snapshot"],
        "common_tool_versions": toolchain["common_tool_versions"],
        "accela_jdk_version": toolchain["accela_jdk_version"],
        "baselines": [
            {
                key: baseline[key]
                for key in (
                    "compiler_baseline",
                    "profile_id",
                    "profile_sha256",
                    "tool",
                    "version",
                    "optimization",
                    "compiler_command_sha256",
                    "compiler_argv_sha256",
                )
            }
            for baseline in toolchain["baselines"]
        ],
    }


def _candidate_final_diagnostic_ref(
    diagnostic_plan: Mapping[str, Any],
    *,
    campaign_id: str,
    study_path: Path | None,
    workspace_root: Path,
) -> dict[str, Any]:
    study = diagnostic_plan["study"]
    if (study["status"] == "completed") != (study_path is not None):
        raise ValidationError(
            "candidate final diagnostic study path/status binding differs"
        )
    return {
        "source_freeze_sha256": diagnostic_plan["source_freeze_sha256"],
        "source_study_sha256": diagnostic_plan["source_study_sha256"],
        "matrix": diagnostic_plan["matrix"],
        "top3_candidate_ids": diagnostic_plan["top3_candidate_ids"],
        "tasks": diagnostic_plan["tasks"],
        "study_status": study["status"],
        "study_ineligibility_reason": study["ineligibility_reason"],
        "study": (
            None
            if study["status"] != "completed"
            else {
                "study_id": f"{campaign_id}:study:diagnostic",
                "path": _workspace_regular_path(
                    workspace_root,
                    study_path,
                    label="candidate final diagnostic study",
                )[2].as_posix(),
                "canonical_sha256": study["evidence_sha256"],
                "physical_sha256": study["evidence_physical_sha256"],
            }
        ),
    }


def _require_candidate_final_campaign_identity(
    *,
    final: Mapping[str, Any],
    plan: Mapping[str, Any],
    previous: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> None:
    previous_by_id = {item["task_id"]: item for item in previous["tasks"]}
    plan_sha256 = sha256_json(plan)
    previous_sha256 = sha256_json(previous)
    if (
        previous["campaign_id"] != plan["campaign_id"]
        or previous["plan_sha256"] != plan_sha256
        or previous["ready_tasks"] != ["final"]
        or previous_by_id.get("final", {}).get("status") != "pending"
        or final["campaign"]["plan_sha256"] != plan_sha256
        or final["campaign"]["status_sha256"] != previous_sha256
        or final["campaign"]["status_ledger_head_sha256"] != previous_sha256
        or final["campaign"]["raw_evidence_registry"]
        != previous["raw_evidence_registry"]
        or final["freeze"]["freeze_id"] != freeze["freeze_id"]
        or final["freeze"]["freeze_sha256"] != sha256_json(freeze)
        or final["freeze"]["campaign_id"] != plan["campaign_id"]
        or final["freeze"]["run_namespace"] != plan["run_namespace"]
        or final["freeze"]["repo_commit"]
        != plan["repository"]["repo_commit"]
        or final["freeze"]["repo_tree"] != plan["repository"]["repo_tree"]
        or final["freeze"]["compiler_artifact"]
        != plan["repository"]["compiler_artifact"]
        or final["freeze"]["raw_evidence_registry"]
        != freeze["b2_campaign"]["raw_evidence_registry"]
    ):
        raise ValidationError(
            "candidate campaign final differs from its exact campaign/freeze/status"
        )


def _require_exact_candidate_final_derivation(
    final: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    if final != expected:
        raise ValidationError(
            "candidate campaign final differs from exact raw-study derivation"
        )


def update_candidate_campaign_status(
    *,
    plan_path: Path,
    run_paths: Mapping[str, Path],
    study_paths: Mapping[str, Path],
    diagnostic_matrix_path: Path | None = None,
    freeze_path: Path | None = None,
    final_path: Path | None = None,
    raw_evidence_registry_path: Path,
    workspace_root: Path,
    previous_status_path: Path | None = None,
    status_ledger_paths: Sequence[Path] = (),
    started_at: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Recompute the sole dependency-safe B1-through-final scheduler view."""

    plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="candidate campaign plan"
    )
    if (
        plan["run_record_schema_sha256"] != schema_sha256("run-record.v1")
        or plan["candidate_study_schema_sha256"]
        != schema_sha256("candidate-study.v1")
        or plan["candidate_freeze_schema_sha256"]
        != schema_sha256("candidate-freeze.v1")
        or plan["candidate_raw_evidence_schema_sha256"]
        != schema_sha256("candidate-raw-evidence.v1")
    ):
        raise ValidationError("candidate campaign plan binds a stale active schema")
    candidate_ids = plan["qualified_candidate_ids"]
    root = _candidate_workspace_root(workspace_root)
    artifact_versions = {
        "candidate_registry": "candidate-catalog.v1",
        "executable_pass_registry": "pass-registry.v2",
        "screening_base_pass_registry": "pass-registry.v2",
        "matrix": "candidate-profile-matrix.v1",
        "screening": "candidate-screening.v1",
    }
    documents = {
        key: _load_frozen_artifact(
            root,
            plan["artifacts"][key],
            label=f"candidate campaign {key}",
            version=version,
        )
        for key, version in artifact_versions.items()
    }
    catalog = documents["candidate_registry"]
    pass_registry = documents["executable_pass_registry"]
    base_pass_registry = documents["screening_base_pass_registry"]
    matrix = documents["matrix"]
    screening = documents["screening"]
    if (
        _load_and_reverify_candidate_screening(
            screening_path=root / plan["artifacts"]["screening"]["path"],
            workspace_root=root,
        )
        != screening
        or
        screening["base_pass_registry"]
        != plan["artifacts"]["screening_base_pass_registry"]
        or _require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=pass_registry,
            workspace_root=root,
        )
        != base_pass_registry
    ):
        raise ValidationError("candidate campaign base PassRegistry artifact drifted")
    qualified_screening = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    if (
        [item["candidate_id"] for item in catalog["candidates"]] != candidate_ids
        or qualified_screening != candidate_ids
        or matrix["candidate_registry_sha256"] != sha256_json(catalog)
        or matrix["pass_registry_sha256"] != sha256_json(pass_registry)
        or screening["pass_registry_sha256"] != sha256_json(base_pass_registry)
    ):
        raise ValidationError("candidate campaign frozen registry/matrix/screening drifted")
    profiles = _verify_matrix_profiles(
        root, matrix, candidate_order=candidate_ids
    )
    _, observed_tree = _clean_repository_identity(
        root, plan["repository"]["repo_commit"]
    )
    if observed_tree != plan["repository"]["repo_tree"] or _frozen_compiler_artifact(
        root, root / plan["repository"]["compiler_artifact"]["path"]
    ) != plan["repository"]["compiler_artifact"]:
        raise ValidationError("candidate campaign repository/compiler artifact drifted")
    protocol = _load_frozen_artifact(
        root,
        plan["measurement_protocol"]["artifact"],
        label="candidate campaign standard measurement protocol",
        version="measurement-protocol.v1",
    )
    if (
        protocol["measurement_mode"] != "standard_proxy"
        or protocol["protocol_id"] != plan["measurement_protocol"]["protocol_id"]
        or sha256_json(protocol) != plan["measurement_protocol"]["protocol_sha256"]
    ):
        raise ValidationError("candidate campaign measurement protocol drifted")
    base = plan["base_pipeline_profile"]
    if (
        base["profile_id"] != "candidate-empty"
        or base["profile_sha256"] != profiles["candidate-empty"]["profile_sha256"]
        or base["path"] != profiles["candidate-empty"]["path"]
        or base["physical_sha256"]
        != sha256_file(root / base["path"])
    ):
        raise ValidationError("candidate campaign base profile physical identity drifted")
    suite_by_role = {item["data_role"]: item for item in plan["suites"]}
    for role, suite in suite_by_role.items():
        manifest = _load_frozen_artifact(
            root,
            suite["manifest"],
            label=f"candidate campaign {role} manifest",
            version="benchmark-manifest.v1",
        )
        require_formal_suite_contract(
            role=role,
            manifest=manifest,
            manifest_path=root / suite["manifest"]["path"],
        )
        if (
            manifest["suite_id"] != suite["suite_id"]
            or len(manifest["cases"]) != suite["case_count"]
        ):
            raise ValidationError(f"candidate campaign {role} suite metadata drifted")

    tasks = {item["task_id"]: item for item in plan["tasks"]}
    previous = (
        _load_version(
            previous_status_path,
            "candidate-campaign-status.v1",
            label="previous candidate campaign status",
        )
        if previous_status_path is not None
        else None
    )
    plan_sha256 = sha256_json(plan)
    chain_started_at, observation_time, previous_sha256 = campaign_status_chain(
        campaign_id=plan["campaign_id"],
        plan_sha256=plan_sha256,
        previous=previous,
        started_at=started_at,
        as_of=as_of,
    )
    main_run_paths = {key: value for key, value in run_paths.items() if key in tasks}
    diagnostic_run_paths = {
        key: value for key, value in run_paths.items() if key.startswith("diagnostic.")
    }
    unknown = sorted(set(run_paths) - set(main_run_paths) - set(diagnostic_run_paths))
    if unknown:
        raise ConfigurationError("unknown candidate campaign run task: " + ", ".join(unknown))
    if any(tasks[item]["task_type"] != "run" for item in main_run_paths):
        raise ConfigurationError("candidate campaign --run may bind only run tasks")
    raw_evidence_registry, raw_evidence_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=raw_evidence_registry_path,
            workspace_root=root,
            expected_run_paths=run_paths,
        )
    )
    if previous is not None:
        _, previous_raw_registry_artifact = (
            _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=root / previous["raw_evidence_registry"]["path"],
            workspace_root=root,
        )
        )
        if previous_raw_registry_artifact != previous["raw_evidence_registry"]:
            raise ValidationError(
                "previous candidate status raw-evidence artifact differs"
            )
    raw_verifications = _raw_verifications_by_run_id(raw_evidence_registry)
    unknown_studies = sorted(
        set(study_paths) - set(plan["study_ids"]) - {"diagnostic"}
    )
    if unknown_studies:
        raise ConfigurationError(
            "unknown candidate campaign study role: " + ", ".join(unknown_studies)
        )
    statuses_by_id: dict[str, dict[str, Any]] = {}
    loaded_runs: dict[str, dict[str, Any]] = {}
    for task_id, task in tasks.items():
        if task["task_type"] != "run":
            continue
        path = main_run_paths.get(task_id)
        if path is None:
            statuses_by_id[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "started_at": None,
                "completed_at": None,
                "ineligibility_reason": None,
            }
        else:
            run = _load_version(path, "run-record.v1", label=f"campaign run {task_id}")
            _validate_campaign_run(plan, task, run, profiles=profiles)
            binding = _binding_record(run)
            if (
                binding["repo_commit"] != plan["repository"]["repo_commit"]
                or binding["repo_dirty"]
                or binding["tracked_diff_sha256"] is not None
                or binding["measurement_protocol_id"]
                != plan["measurement_protocol"]["protocol_id"]
                or binding["measurement_protocol_sha256"]
                != plan["measurement_protocol"]["protocol_sha256"]
                or (
                    task["kind"] != "reference"
                    and binding["compiler_artifact_sha256"]
                    != plan["repository"]["compiler_artifact"]["physical_sha256"]
                )
            ):
                raise ValidationError(
                    f"candidate campaign run differs from frozen revision/artifact/protocol: {task_id}"
                )
            loaded_runs[task_id] = run
            status, _ = campaign_run_status(run)
            statuses_by_id[task_id] = {
                "task_id": task_id,
                "status": status,
                "evidence_kind": "run-record.v1",
                "evidence_sha256": sha256_json(run),
                "evidence_physical_sha256": sha256_file(path),
                "started_at": run["started_at"],
                "completed_at": raw_verifications[run["run_id"]][
                    "terminal_observed_at"
                ],
                "ineligibility_reason": None,
            }
    b1_passed = {
        candidate_id
        for candidate_id in candidate_ids
        if statuses_by_id[f"run.B1.{candidate_id}"]["status"] == "completed"
    }

    loaded_studies: dict[str, dict[str, Any]] = {}
    promoted: set[str] = set()
    for role in ("B2", "B3", "B4", "B5", "B6"):
        task_id = f"study.{role}"
        path = study_paths.get(role)
        if path is None:
            statuses_by_id[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "started_at": None,
                "completed_at": None,
                "ineligibility_reason": None,
            }
            continue
        study = _load_version(
            path, "candidate-study.v1", label=f"candidate campaign {role} study"
        )
        suite = suite_by_role[role]
        if (
            study["study_id"] != plan["study_ids"][role]
            or study["data_role"] != role
            or study["matrix_sha256"] != plan["artifacts"]["matrix"]["canonical_sha256"]
            or study["candidate_registry_sha256"]
            != plan["artifacts"]["candidate_registry"]["canonical_sha256"]
            or study["pass_registry_sha256"]
            != plan["artifacts"]["executable_pass_registry"]["canonical_sha256"]
            or study["suite_id"] != suite["suite_id"]
            or study["manifest_sha256"] != suite["manifest"]["canonical_sha256"]
        ):
            raise ValidationError(f"candidate campaign {role} study binding differs")
        expected_ids = (
            b1_passed if role in {"B2", "B3"} else promoted
        )
        result_by_id = {item["candidate_id"]: item for item in study["candidates"]}
        if set(result_by_id) != expected_ids:
            raise ValidationError(
                f"candidate campaign {role} study candidate gate differs"
            )
        baseline_task_id = f"run.{role}.full"
        baseline_run = loaded_runs.get(baseline_task_id)
        if baseline_run is None or study["baseline"] != _run_ref(baseline_run):
            raise ValidationError(
                f"candidate campaign {role} study does not bind its FULL run"
            )
        for candidate_id, result in result_by_id.items():
            run_task_id = f"run.{role}.{candidate_id}"
            run = loaded_runs.get(run_task_id)
            if run is None or (
                result["run_id"] != run["run_id"]
                or result["run_sha256"] != sha256_json(run)
                or result["configuration_sha256"] != run["configuration_sha256"]
            ):
                raise ValidationError(
                    f"candidate campaign study/run SHA binding differs: {run_task_id}"
                )
        _validate_study_against_raw_runs(
            study=study,
            baseline=baseline_run,
            candidate_runs={
                candidate_id: loaded_runs[f"run.{role}.{candidate_id}"]
                for candidate_id in result_by_id
            },
            interaction_runs={},
            catalog=catalog,
            raw_verifications=raw_verifications,
        )
        if role == "B3":
            if study["interactions"]:
                raise ValidationError(
                    "formal B3 study cannot contain post-B3 diagnostic pair evidence"
                )
            promoted = {
                candidate_id
                for candidate_id, result in result_by_id.items()
                if result["eligible_for_ranking"]
                and result["case_geometric_mean_speedup"] is not None
                and result["case_geometric_mean_speedup"] > 1.0
            }
        loaded_studies[role] = study
        statuses_by_id[task_id] = {
            "task_id": task_id,
            "status": "completed",
            "evidence_kind": "candidate-study.v1",
            "evidence_sha256": sha256_json(study),
            "evidence_physical_sha256": sha256_file(path),
            "started_at": study["generated_at"],
            "completed_at": study["generated_at"],
            "ineligibility_reason": None,
        }

    freeze: dict[str, Any] | None = None
    if freeze_path is None:
        statuses_by_id["freeze"] = {
            "task_id": "freeze",
            "status": "pending",
            "evidence_kind": None,
            "evidence_sha256": None,
            "evidence_physical_sha256": None,
            "started_at": None,
            "completed_at": None,
            "ineligibility_reason": None,
        }
    else:
        freeze = _load_version(
            freeze_path, "candidate-freeze.v1", label="candidate campaign freeze"
        )
        _verify_candidate_freeze_inputs(
            freeze, workspace_root=root, candidate_order=candidate_ids
        )
        if (
            freeze["campaign_id"] != plan["campaign_id"]
            or freeze["b2_campaign"]["plan_sha256"] != plan_sha256
            or freeze["b2_campaign"]["study_sha256"]
            != statuses_by_id["study.B2"]["evidence_sha256"]
        ):
            raise ValidationError("candidate campaign freeze differs from plan/B2 study")
        _, frozen_raw_registry_artifact = (
            _load_and_reverify_candidate_raw_evidence_registry(
                plan=plan,
                registry_path=root
                / freeze["b2_campaign"]["raw_evidence_registry"]["path"],
                workspace_root=root,
            )
        )
        if (
            frozen_raw_registry_artifact
            != freeze["b2_campaign"]["raw_evidence_registry"]
            or (
                previous is not None
                and previous["tasks"][
                    next(
                        index
                        for index, row in enumerate(previous["tasks"])
                        if row["task_id"] == "freeze"
                    )
                ]["status"]
                == "pending"
                and previous["raw_evidence_registry"]
                != frozen_raw_registry_artifact
            )
        ):
            raise ValidationError(
                "candidate campaign freeze raw-evidence binding differs"
            )
        statuses_by_id["freeze"] = {
            "task_id": "freeze",
            "status": "completed",
            "evidence_kind": "candidate-freeze.v1",
            "evidence_sha256": sha256_json(freeze),
            "evidence_physical_sha256": sha256_file(freeze_path),
            "started_at": freeze["frozen_at"],
            "completed_at": freeze["frozen_at"],
            "ineligibility_reason": None,
        }
        for task_id, run in loaded_runs.items():
            task = tasks[task_id]
            if task["kind"] == "reference":
                _require_frozen_reference_run(run, task, freeze)

    final: dict[str, Any] | None = None
    if final_path is None:
        statuses_by_id["final"] = {
            "task_id": "final",
            "status": "pending",
            "evidence_kind": None,
            "evidence_sha256": None,
            "evidence_physical_sha256": None,
            "started_at": None,
            "completed_at": None,
            "ineligibility_reason": None,
        }
    else:
        final = _load_version(
            final_path, "candidate-final.v1", label="candidate campaign final"
        )
        if freeze is None or previous is None:
            raise ValidationError(
                "candidate campaign final requires its freeze and exact pre-final status"
            )
        if raw_evidence_registry_artifact != previous["raw_evidence_registry"]:
            raise ValidationError(
                "candidate final registration must reuse the exact pre-final raw snapshot"
            )
        _require_candidate_final_campaign_identity(
            final=final,
            plan=plan,
            previous=previous,
            freeze=freeze,
        )
        final_freeze = final["freeze"]
        frozen_snapshots = freeze["snapshots"]
        frozen_suites = {
            item["data_role"]: item for item in freeze["suites"]
        }
        if (
            final["screening_sha256"] != sha256_json(screening)
            or final["candidate_registry_sha256"] != sha256_json(catalog)
            or final["matrix_sha256"] != sha256_json(matrix)
            or final_freeze["candidate_registry"]
            != frozen_snapshots["candidate_registry"]
            or final_freeze["executable_pass_registry"]
            != frozen_snapshots["executable_pass_registry"]
            or final_freeze["screening_base_pass_registry"]
            != frozen_snapshots["screening_base_pass_registry"]
            or final_freeze["matrix"] != frozen_snapshots["matrix"]
            or final_freeze["screening"] != frozen_snapshots["screening"]
            or final_freeze["oracle_capture"]
            != frozen_snapshots["oracle_capture"]
            or final_freeze["run_record_schema"]
            != frozen_snapshots["run_record_schema"]
            or final_freeze["candidate_study_schema"]
            != frozen_snapshots["candidate_study_schema"]
            or final_freeze["base_pipeline_profile"]
            != freeze["base_pipeline_profile"]["artifact"]
            or final_freeze["standard_measurement_protocol"]
            != {
                "path": freeze["measurement_protocols"]["standard_proxy"]["path"],
                "canonical_sha256": freeze["measurement_protocols"][
                    "standard_proxy"
                ]["protocol_sha256"],
                "physical_sha256": freeze["measurement_protocols"][
                    "standard_proxy"
                ]["physical_sha256"],
            }
            or final_freeze["hotblock_measurement_protocol"]
            != {
                "path": freeze["measurement_protocols"]["cache_hotblock"]["path"],
                "canonical_sha256": freeze["measurement_protocols"][
                    "cache_hotblock"
                ]["protocol_sha256"],
                "physical_sha256": freeze["measurement_protocols"][
                    "cache_hotblock"
                ]["physical_sha256"],
            }
            or final_freeze["suite_manifests"]
            != {
                role: frozen_suites[role]["manifest"]
                for role in _CANDIDATE_SUITE_CASE_COUNTS
            }
            or final_freeze["reference_toolchain"]
            != _candidate_reference_toolchain_context(freeze)
            or final_freeze["frozen_candidate_ids_sha256"]
            != freeze["frozen_candidate_ids_sha256"]
            or final_freeze["freeze_status_ledger_entry_count"]
            != freeze["b2_campaign"]["status_ledger_entry_count"]
            or final_freeze["freeze_status_ledger_head_sha256"]
            != freeze["b2_campaign"]["status_ledger_head_sha256"]
            or final_freeze["freeze_status_ledger_sha256"]
            != freeze["b2_campaign"]["status_ledger_sha256"]
            or final_freeze["b1_full_run_sha256"]
            != freeze["b2_campaign"]["b1_full_run_sha256"]
            or final_freeze["b1_passed_candidate_ids"]
            != freeze["b2_campaign"]["b1_passed_candidate_ids"]
            or final_freeze["b1_failed_candidate_ids"]
            != freeze["b2_campaign"]["b1_failed_candidate_ids"]
            or final_freeze["oracle_threshold"]
            != freeze["gates"]["oracle_structure_geometric_mean_minimum"]
            or final_freeze["b3_strict_threshold"]
            != freeze["gates"]["b3_geometric_mean_strictly_above"]
            or final_freeze["combined_case_count"]
            != freeze["gates"]["combined_case_count"]
            or final_freeze["ranking_rule"]
            != [
                freeze["ranking_rule"]["primary"],
                freeze["ranking_rule"]["secondary"],
                freeze["ranking_rule"]["tertiary"],
                freeze["ranking_rule"]["quaternary"],
            ]
        ):
            raise ValidationError(
                "candidate campaign final differs from its exact campaign/freeze/status"
            )
        statuses_by_id["final"] = {
            "task_id": "final",
            "status": "completed",
            "evidence_kind": "candidate-final.v1",
            "evidence_sha256": sha256_json(final),
            "evidence_physical_sha256": sha256_file(final_path),
            "started_at": final["generated_at"],
            "completed_at": final["generated_at"],
            "ineligibility_reason": None,
        }

    b3_study = loaded_studies.get("B3")
    for task in plan["tasks"]:
        row = statuses_by_id[task["task_id"]]
        gate = task["gate"]
        gate_false_reason: str | None = None
        gate_decided_at: str | None = None
        if gate["kind"] == "b1_completed":
            source = statuses_by_id[f"run.B1.{gate['candidate_id']}"]
            if source["status"] in {"failed", "interrupted"}:
                gate_false_reason = "b1_not_completed_correct"
                gate_decided_at = source["completed_at"]
        elif gate["kind"] == "b3_promoted" and b3_study is not None:
            if gate["candidate_id"] not in promoted:
                gate_false_reason = "b3_not_strictly_above_one"
                gate_decided_at = b3_study["generated_at"]
        elif gate["kind"] == "b3_has_promoted" and b3_study is not None:
            if not promoted:
                gate_false_reason = "no_b3_promotion"
                gate_decided_at = b3_study["generated_at"]
        elif gate["kind"] == "b2_formal_complete" and "B2" in loaded_studies:
            if any(
                not item["eligible_for_ranking"]
                for item in loaded_studies["B2"]["candidates"]
            ):
                gate_false_reason = "b2_not_formal_complete"
                gate_decided_at = loaded_studies["B2"]["generated_at"]
        if gate_false_reason is not None:
            if row["evidence_sha256"] is not None:
                raise ValidationError(
                    f"candidate campaign leapfrog supplied gated evidence: {task['task_id']}"
                )
            statuses_by_id[task["task_id"]] = {
                **row,
                "status": "ineligible",
                "completed_at": gate_decided_at,
                "ineligibility_reason": gate_false_reason,
            }

    terminal_states = {"completed", "failed", "interrupted", "ineligible"}
    for task in plan["tasks"]:
        row = statuses_by_id[task["task_id"]]
        if row["evidence_sha256"] is None:
            continue
        if any(
            statuses_by_id[item]["status"] != "completed"
            for item in task["dependencies"]
        ) or any(
            statuses_by_id[item]["status"] not in terminal_states
            for item in task["terminal_dependencies"]
        ):
            raise ValidationError(
                f"candidate campaign evidence leapfrogs dependencies: {task['task_id']}"
            )

    statuses = [statuses_by_id[item["task_id"]] for item in plan["tasks"]]
    def parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    campaign_start = parse_time(chain_started_at)
    observation = parse_time(observation_time)
    for row in statuses:
        started_value = row["started_at"]
        completed_value = row["completed_at"]
        if started_value is not None:
            started_time = parse_time(started_value)
            if started_time < campaign_start or started_time > observation:
                raise ValidationError(
                    f"candidate campaign task start lies outside the ledger clock: {row['task_id']}"
                )
        else:
            started_time = None
        if completed_value is not None:
            completed_time = parse_time(completed_value)
            if completed_time > observation or (
                started_time is not None and completed_time < started_time
            ):
                raise ValidationError(
                    f"candidate campaign task completion violates the ledger clock: {row['task_id']}"
                )
    for task in plan["tasks"]:
        row = statuses_by_id[task["task_id"]]
        if row["started_at"] is None:
            continue
        started_time = parse_time(row["started_at"])
        for dependency in [*task["dependencies"], *task["terminal_dependencies"]]:
            dependency_completed = statuses_by_id[dependency]["completed_at"]
            if dependency_completed is None or parse_time(dependency_completed) > started_time:
                raise ValidationError(
                    f"candidate campaign dependency completes after dependent start: {task['task_id']}"
                )
    if previous is not None:
        enforce_terminal_task_immutability(
            previous_rows=previous["tasks"],
            current_rows=statuses,
            label="candidate campaign",
        )
    ready_tasks = ready_campaign_task_ids(tasks=plan["tasks"], statuses=statuses)

    diagnostic_plan: dict[str, Any] | None = None
    diagnostic_loaded_runs: dict[str, dict[str, Any]] = {}
    if b3_study is not None:
        if freeze is None or diagnostic_matrix_path is None:
            raise ConfigurationError(
                "completed B3 requires its freeze-bound diagnostic profile matrix"
            )
        diagnostic_matrix = _load_version(
            diagnostic_matrix_path,
            "candidate-profile-matrix.v1",
            label="candidate B3 diagnostic matrix",
        )
        if (
            diagnostic_matrix["candidate_registry_sha256"] != sha256_json(catalog)
            or diagnostic_matrix["pass_registry_sha256"] != sha256_json(pass_registry)
        ):
            raise ValidationError("candidate diagnostic matrix registry binding differs")
        diagnostic_profiles = _verify_matrix_profiles(
            root, diagnostic_matrix, candidate_order=candidate_ids
        )
        top3 = [
            item["candidate_id"]
            for item in sorted(
                (
                    item
                    for item in b3_study["candidates"]
                    if item["eligible_for_ranking"]
                ),
                key=lambda item: (
                    -float(item["case_geometric_mean_speedup"]),
                    item["candidate_id"],
                ),
            )[:3]
        ]
        expected_pairs = {
            frozenset(pair) for pair in combinations(top3, 2)
        }
        pair_records = {
            frozenset(item["candidate_ids"]): item
            for item in diagnostic_matrix["profiles"]
            if item["kind"] == "pair"
        }
        scheduled_pairs = {
            frozenset(item["candidate_ids"])
            for item in diagnostic_matrix["schedule"]
            if item["kind"] == "pair"
        }
        if set(pair_records) != expected_pairs or scheduled_pairs != expected_pairs:
            raise ValidationError(
                "candidate diagnostic matrix must contain all and only exact B3 Top3 pairs"
            )
        single_records = {
            item["candidate_ids"][0]: item
            for item in diagnostic_matrix["profiles"]
            if item["kind"] == "single"
        }
        if set(single_records) != set(candidate_ids):
            raise ValidationError("candidate diagnostic matrix single profiles differ")
        diagnostic_specs: list[tuple[str, str, list[str]]] = []
        for left, right in combinations(top3, 2):
            profile_record = pair_records[frozenset((left, right))]
            profile_candidate_ids = list(profile_record["candidate_ids"])
            stable_pair_id = "+".join(sorted((left, right)))
            diagnostic_specs.append(
                (
                    f"diagnostic.pair.{stable_pair_id}",
                    "pair",
                    profile_candidate_ids,
                )
            )
        diagnostic_specs.extend(
            (
                "diagnostic.cache.full"
                if candidate_id is None
                else f"diagnostic.cache.{candidate_id}",
                "cache_hotblock",
                [] if candidate_id is None else [candidate_id],
            )
            for candidate_id in [None, *top3]
        )
        diagnostic_tasks: list[dict[str, Any]] = []
        diagnostic_dependency = "study.B3"
        for task_id, kind, diagnostic_candidate_ids in diagnostic_specs:
            if kind == "pair":
                profile_record = pair_records[frozenset(diagnostic_candidate_ids)]
                measurement_mode = "standard_proxy"
            elif diagnostic_candidate_ids:
                profile_record = single_records[diagnostic_candidate_ids[0]]
                measurement_mode = "cache_hotblock"
            else:
                profile_record = diagnostic_matrix["profiles"][0]
                if profile_record["kind"] != "candidate_empty":
                    raise ValidationError("candidate diagnostic matrix lacks leading FULL")
                measurement_mode = "cache_hotblock"
            path = diagnostic_run_paths.get(task_id)
            if path is None:
                status = "pending"
                evidence_sha256 = None
                evidence_physical_sha256 = None
                configuration_sha256 = None
                started_at_value = None
                completed_at_value = None
                failure_reason = None
            else:
                run = _load_version(
                    path,
                    "run-record.v1",
                    label=f"candidate diagnostic run {task_id}",
                )
                _validate_candidate_diagnostic_run(
                    task={
                        "task_id": task_id,
                        "run_id": f"{plan['run_namespace']}{task_id}",
                        "measurement_mode": measurement_mode,
                        "candidate_ids": diagnostic_candidate_ids,
                    },
                    run=run,
                    profile=diagnostic_profiles[profile_record["profile_id"]],
                    candidate_registry_sha256=sha256_json(catalog),
                    pass_registry_sha256=sha256_json(pass_registry),
                    b3_suite=suite_by_role["B3"],
                    freeze=freeze,
                )
                status, failure_reason = campaign_run_status(run)
                diagnostic_loaded_runs[task_id] = run
                evidence_sha256 = sha256_json(run)
                evidence_physical_sha256 = sha256_file(path)
                configuration_sha256 = run["configuration_sha256"]
                started_at_value = run["started_at"]
                completed_at_value = raw_verifications[run["run_id"]][
                    "terminal_observed_at"
                ]
            diagnostic_tasks.append(
                {
                    "task_id": task_id,
                    "kind": kind,
                    "candidate_ids": diagnostic_candidate_ids,
                    "run_id": f"{plan['run_namespace']}{task_id}",
                    "data_role": "B3",
                    "measurement_mode": measurement_mode,
                    "logical_profile_id": profile_record["profile_id"],
                    "profile_sha256": profile_record["profile_sha256"],
                    "configuration_sha256": configuration_sha256,
                    "profile_path": profile_record["path"],
                    "ranking_evidence": False,
                    "dependencies": [diagnostic_dependency],
                    "status": status,
                    "evidence_sha256": evidence_sha256,
                    "evidence_physical_sha256": evidence_physical_sha256,
                    "started_at": started_at_value,
                    "completed_at": completed_at_value,
                    "failure_reason": failure_reason,
                }
            )
            diagnostic_dependency = task_id
        unknown_diagnostic = sorted(set(diagnostic_run_paths) - {
            item["task_id"] for item in diagnostic_tasks
        })
        if unknown_diagnostic:
            raise ConfigurationError(
                "unknown candidate diagnostic task: " + ", ".join(unknown_diagnostic)
            )
        diagnostic_study_path = study_paths.get("diagnostic")
        raw_diagnostics_terminal = all(
            item["status"] in {"completed", "failed", "interrupted"}
            for item in diagnostic_tasks
        )
        pair_tasks = [item for item in diagnostic_tasks if item["kind"] == "pair"]
        automatic_study_ineligibility = (
            "fewer_than_two_top3"
            if not pair_tasks
            else None
        )
        if automatic_study_ineligibility is not None:
            if diagnostic_study_path is not None:
                raise ValidationError(
                    "candidate diagnostic study is forbidden without analyzable pair evidence"
                )
            diagnostic_study_task = {
                "task_id": "diagnostic.study",
                "dependencies": [diagnostic_dependency],
                "status": "ineligible" if raw_diagnostics_terminal else "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "started_at": None,
                "completed_at": (
                    diagnostic_tasks[-1]["completed_at"]
                    if raw_diagnostics_terminal
                    else None
                ),
                "ineligibility_reason": (
                    automatic_study_ineligibility
                    if raw_diagnostics_terminal
                    else None
                ),
            }
        elif diagnostic_study_path is None:
            diagnostic_study_task = {
                "task_id": "diagnostic.study",
                "dependencies": [diagnostic_dependency],
                "status": "pending",
                "evidence_kind": None,
                "evidence_sha256": None,
                "evidence_physical_sha256": None,
                "started_at": None,
                "completed_at": None,
                "ineligibility_reason": None,
            }
        else:
            if not raw_diagnostics_terminal:
                raise ValidationError(
                    "candidate diagnostic study leapfrogs unfinished raw diagnostics"
                )
            diagnostic_study = _load_version(
                diagnostic_study_path,
                "candidate-study.v1",
                label="candidate diagnostic interaction study",
            )
            if diagnostic_study["study_id"] != f"{plan['campaign_id']}:study:diagnostic":
                raise ValidationError("candidate diagnostic study id differs")
            pair_runs = {
                frozenset(task["candidate_ids"]): (
                    task,
                    diagnostic_loaded_runs[task["task_id"]],
                )
                for task in pair_tasks
            }
            _validate_candidate_diagnostic_study(
                study=diagnostic_study,
                formal_b3_study=b3_study,
                baseline=loaded_runs["run.B3.full"],
                candidate_runs={
                    candidate_id: loaded_runs[f"run.B3.{candidate_id}"]
                    for candidate_id in top3
                },
                pair_runs=pair_runs,
                top3_candidate_ids=top3,
                matrix_sha256=sha256_json(diagnostic_matrix),
                catalog=catalog,
                raw_verifications=raw_verifications,
            )
            diagnostic_study_task = {
                "task_id": "diagnostic.study",
                "dependencies": [diagnostic_dependency],
                "status": "completed",
                "evidence_kind": "candidate-study.v1",
                "evidence_sha256": sha256_json(diagnostic_study),
                "evidence_physical_sha256": sha256_file(diagnostic_study_path),
                "started_at": diagnostic_study["generated_at"],
                "completed_at": diagnostic_study["generated_at"],
                "ineligibility_reason": None,
            }
        diagnostic_plan = {
            "source_freeze_sha256": statuses_by_id["freeze"]["evidence_sha256"],
            "source_study_sha256": sha256_json(b3_study),
            "matrix": _frozen_artifact_digest(
                root,
                diagnostic_matrix_path,
                diagnostic_matrix,
                label="candidate diagnostic matrix",
            ),
            "top3_candidate_ids": top3,
            "tasks": diagnostic_tasks,
            "study": diagnostic_study_task,
        }
    elif diagnostic_matrix_path is not None or diagnostic_run_paths:
        raise ConfigurationError("candidate diagnostics require an exact completed B3 study")

    if previous is not None:
        previous_diagnostic = previous["diagnostic_plan"]
        if previous_diagnostic is not None and diagnostic_plan is None:
            raise ValidationError("candidate diagnostic child plan cannot disappear")
        if previous_diagnostic is not None and diagnostic_plan is not None:
            if any(
                previous_diagnostic[key] != diagnostic_plan[key]
                for key in (
                    "source_freeze_sha256",
                    "source_study_sha256",
                    "matrix",
                    "top3_candidate_ids",
                )
            ):
                raise ValidationError("candidate diagnostic child plan identity is immutable")
            enforce_terminal_task_immutability(
                previous_rows=previous_diagnostic["tasks"],
                current_rows=diagnostic_plan["tasks"],
                label="candidate diagnostic campaign",
            )
            enforce_terminal_task_immutability(
                previous_rows=[previous_diagnostic["study"]],
                current_rows=[diagnostic_plan["study"]],
                label="candidate diagnostic study",
            )

    if final is not None:
        if diagnostic_plan is None:
            raise ValidationError(
                "candidate campaign final lacks its exact diagnostic child plan"
            )
        if (
            previous_status_path is None
            or not status_ledger_paths
            or freeze_path is None
            or "B2" not in study_paths
        ):
            raise ConfigurationError(
                "candidate campaign final registration requires the ordered "
                "pre-final ledger and exact B2/freeze inputs"
            )
        ordered_run_ids = [
            *[
                task["task_id"]
                for task in plan["tasks"]
                if task["task_id"] in loaded_runs
            ],
            *[
                task["task_id"]
                for task in diagnostic_plan["tasks"]
                if task["task_id"] in diagnostic_loaded_runs
            ],
        ]
        all_loaded_runs = {**loaded_runs, **diagnostic_loaded_runs}
        expected_run_records = [
            {
                "task_id": task_id,
                "run_id": all_loaded_runs[task_id]["run_id"],
                "run_sha256": sha256_json(all_loaded_runs[task_id]),
                "run_physical_sha256": sha256_file(run_paths[task_id]),
                "state": all_loaded_runs[task_id]["state"],
            }
            for task_id in ordered_run_ids
        ]
        expected_studies = {
            role: (
                None
                if role not in loaded_studies
                else _candidate_final_study_ref(
                    loaded_studies[role],
                    study_path=study_paths[role],
                    workspace_root=root,
                )
            )
            for role in ("B2", "B3", "B4", "B5", "B6")
        }
        b1_full = loaded_runs.get("run.B1.full")
        if b1_full is None:
            raise ValidationError("candidate campaign final lacks B1 FULL evidence")
        expected_b1_full = {
            "run_id": b1_full["run_id"],
            "run_sha256": sha256_json(b1_full),
            "configuration_sha256": b1_full["configuration_sha256"],
            "suite_id": b1_full["suite_id"],
            "manifest_sha256": b1_full["manifest_sha256"],
            "case_count": b1_full["manifest_case_count"],
            "evidence_level": b1_full["configuration"]["evidence_level"],
            "state": b1_full["state"],
            "passed_cases": b1_full["summary"]["passed_cases"],
            "failed_cases": b1_full["summary"]["failed_cases"],
            "pending_cases": b1_full["summary"]["pending_cases"],
            "censored_cases": b1_full["summary"]["censored_cases"],
            "all_correct": (
                b1_full["state"] == "completed"
                and b1_full["summary"]["failed_cases"] == 0
                and b1_full["summary"]["pending_cases"] == 0
            ),
            "failure_reason": None,
        }
        if (
            final["campaign"]["run_records"] != expected_run_records
            or final["studies"] != expected_studies
            or final["freeze"]["b2_study_sha256"]
            != sha256_json(loaded_studies["B2"])
            or final["freeze"]["artifact"]
            != _frozen_artifact_digest(
                root,
                freeze_path,
                freeze,
                label="candidate final freeze",
            )
            or final["diagnostics"]
            != _candidate_final_diagnostic_ref(
                diagnostic_plan,
                campaign_id=plan["campaign_id"],
                study_path=diagnostic_study_path,
                workspace_root=root,
            )
            or final["b1_full_correctness"] != expected_b1_full
        ):
            raise ValidationError(
                "candidate campaign final raw/study/diagnostic bindings differ"
            )
        expected_final = build_candidate_final(
            screening_path=root / plan["artifacts"]["screening"]["path"],
            catalog_path=root
            / plan["artifacts"]["candidate_registry"]["path"],
            matrix_path=root / plan["artifacts"]["matrix"]["path"],
            workspace_root=root,
            campaign_plan_path=plan_path,
            campaign_status_path=previous_status_path,
            status_ledger_paths=status_ledger_paths,
            run_paths=run_paths,
            b2_study_path=study_paths["B2"],
            study_paths={
                role: study_paths[role]
                for role in ("B3", "B4", "B5", "B6")
                if role in study_paths
            },
            diagnostic_study_path=study_paths.get("diagnostic"),
            freeze_path=freeze_path,
            final_id=final["final_id"],
        )
        _require_exact_candidate_final_derivation(final, expected_final)

    if diagnostic_plan is not None:
        previous_terminal_time = statuses_by_id["study.B3"]["completed_at"]
        diagnostic_ready: list[str] = []
        diagnostic_active = False
        for task in [*diagnostic_plan["tasks"], diagnostic_plan["study"]]:
            if task["started_at"] is not None:
                if (
                    previous_terminal_time is None
                    or parse_time(task["started_at"])
                    < parse_time(previous_terminal_time)
                ):
                    raise ValidationError(
                        f"candidate diagnostic task starts before its dependency: {task['task_id']}"
                    )
            if task["status"] in {
                "completed",
                "failed",
                "interrupted",
                "ineligible",
            }:
                previous_terminal_time = task["completed_at"]
                continue
            diagnostic_active = True
            if task["status"] == "pending" and not diagnostic_ready:
                diagnostic_ready = [task["task_id"]]
            break
        if diagnostic_active:
            for task in plan["tasks"]:
                if task["stage"] in {"B4", "B5", "B6", "final"} and statuses_by_id[
                    task["task_id"]
                ]["evidence_sha256"] is not None:
                    raise ValidationError(
                        "candidate ranking evidence leapfrogs unfinished diagnostics"
                    )
            ready_tasks = diagnostic_ready

    final_status = statuses_by_id["final"]["status"]
    if final_status == "completed":
        state = "completed"
        ready_tasks = []
    elif statuses_by_id["freeze"]["status"] == "ineligible":
        state = "failed"
    elif not any(row["evidence_sha256"] is not None for row in statuses):
        state = "pending"
    elif ready_tasks:
        state = "running"
    else:
        pending = {row["task_id"] for row in statuses if row["status"] == "pending"}
        blockers = {
            dependency
            for task in plan["tasks"]
            if task["task_id"] in pending
            for dependency in [*task["dependencies"], *task["terminal_dependencies"]]
            if statuses_by_id[dependency]["status"] in {"failed", "interrupted"}
        }
        if any(statuses_by_id[item]["status"] == "interrupted" for item in blockers):
            state = "interrupted"
        elif blockers:
            state = "failed"
        else:
            state = "running"
    if state in {"completed", "failed", "interrupted"}:
        ready_tasks = []
    return validate_document(
        {
            "schema_version": "candidate-campaign-status.v1",
            "campaign_id": plan["campaign_id"],
            "plan_sha256": plan_sha256,
            "previous_status_sha256": previous_sha256,
            "state": state,
            "started_at": chain_started_at,
            "as_of": observation_time,
            "raw_evidence_registry": raw_evidence_registry_artifact,
            "tasks": statuses,
            "ready_tasks": ready_tasks,
            "diagnostic_plan": diagnostic_plan,
        }
    )


def _clean_repository_identity(
    workspace_root: Path,
    expected_commit: str | None = None,
) -> tuple[str, str]:
    root = _candidate_workspace_root(workspace_root)

    def git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ("git", "-C", str(root), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigurationError(
                "candidate freeze Git provenance command did not complete"
            ) from exc
        if result.returncode != 0:
            raise ConfigurationError(
                "candidate freeze Git provenance command failed"
            )
        try:
            return result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeError as exc:
            raise ValidationError(
                "candidate freeze Git provenance output is not UTF-8"
            ) from exc

    head = git("rev-parse", "--verify", "HEAD").lower()
    if expected_commit is not None and head != expected_commit.lower():
        raise ValidationError("candidate freeze workspace HEAD differs from B2 evidence")
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        raise ValidationError("candidate freeze requires a clean workspace")
    tree = git("rev-parse", "--verify", "HEAD^{tree}").lower()
    if len(tree) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in tree
    ):
        raise ValidationError("candidate freeze repository tree id is malformed")
    return head, tree


def _validate_candidate_status_ledger(
    *,
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
    ledger_paths: Sequence[Path],
    workspace_root: Path,
) -> tuple[
    int,
    str,
    str,
    tuple[str, ...],
    tuple[dict[str, str], ...],
]:
    if not ledger_paths:
        raise ConfigurationError("candidate campaign requires the complete status ledger")
    plan_digest = sha256_json(plan)
    previous: dict[str, Any] | None = None
    started_at: str | None = None
    entries: list[dict[str, Any]] = []
    identities: list[dict[str, str]] = []
    for index, path in enumerate(ledger_paths):
        physical_path = resolve_without_symlinks(
            path, label=f"candidate status ledger entry {index}"
        )
        if not physical_path.is_file():
            raise ValidationError(
                f"candidate status ledger entry {index} must be a regular file"
            )
        entry = _load_version(
            physical_path,
            "candidate-campaign-status.v1",
            label=f"candidate status ledger entry {index}",
        )
        if (
            entry["campaign_id"] != plan["campaign_id"]
            or entry["plan_sha256"] != plan_digest
        ):
            raise ValidationError("candidate status ledger entry binds another plan")
        _, raw_registry_artifact = (
            _load_and_reverify_candidate_raw_evidence_registry(
                plan=plan,
                registry_path=_candidate_workspace_root(workspace_root)
                / entry["raw_evidence_registry"]["path"],
                workspace_root=workspace_root,
            )
        )
        if raw_registry_artifact != entry["raw_evidence_registry"]:
            raise ValidationError(
                "candidate status ledger raw-evidence artifact differs"
            )
        if previous is None:
            if entry["previous_status_sha256"] is not None:
                raise ValidationError("candidate status ledger does not start at genesis")
            started_at = entry["started_at"]
        else:
            if (
                entry["previous_status_sha256"] != sha256_json(previous)
                or entry["started_at"] != started_at
                or datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
                <= datetime.fromisoformat(previous["as_of"].replace("Z", "+00:00"))
            ):
                raise ValidationError("candidate status ledger hash/time chain differs")
            enforce_terminal_task_immutability(
                previous_rows=previous["tasks"],
                current_rows=entry["tasks"],
                label="candidate status ledger",
            )
        previous = entry
        entries.append(entry)
        identities.append(
            {
                "canonical_sha256": sha256_json(entry),
                "physical_sha256": sha256_file(physical_path),
            }
        )
    assert previous is not None
    if previous != status:
        raise ValidationError("candidate status ledger head differs from --status")
    return (
        len(entries),
        sha256_json(previous),
        sha256_json(identities),
        tuple(sha256_json(entry) for entry in entries),
        tuple(identities),
    )


def _require_candidate_ledger_prefix(
    *,
    binding: Mapping[str, Any],
    ledger_identities: Sequence[Mapping[str, str]],
    label: str,
) -> list[Mapping[str, str]]:
    prefix_count = binding["status_ledger_entry_count"]
    prefix = list(ledger_identities[:prefix_count])
    if (
        len(prefix) != prefix_count
        or not prefix
        or binding["status_sha256"] != prefix[-1]["canonical_sha256"]
        or binding["status_ledger_head_sha256"]
        != prefix[-1]["canonical_sha256"]
        or binding["status_ledger_sha256"] != sha256_json(prefix)
    ):
        raise ValidationError(
            f"{label} canonical/physical status-ledger prefix differs"
        )
    return prefix


def validate_candidate_final_completion(
    *,
    campaign_plan_path: Path,
    candidate_final_path: Path,
    completed_status_path: Path,
    status_ledger_paths: Sequence[Path],
    workspace_root: Path,
) -> dict[str, Any]:
    """Verify the post-final terminal ledger before publishing a final report."""

    root = _candidate_workspace_root(workspace_root)
    _, plan_physical, _ = _workspace_regular_path(
        root, campaign_plan_path, label="candidate report campaign plan"
    )
    _, final_physical, _ = _workspace_regular_path(
        root, candidate_final_path, label="candidate report final"
    )
    _, status_physical, _ = _workspace_regular_path(
        root, completed_status_path, label="candidate report completed status"
    )
    ledger_physical = [
        _workspace_regular_path(
            root, path, label=f"candidate report status ledger entry {index}"
        )[1]
        for index, path in enumerate(status_ledger_paths)
    ]
    plan = _load_version(
        plan_physical,
        "candidate-campaign-plan.v1",
        label="candidate report campaign plan",
    )
    final = _load_version(
        final_physical, "candidate-final.v1", label="candidate report final"
    )
    completed_status = _load_version(
        status_physical,
        "candidate-campaign-status.v1",
        label="candidate report completed status",
    )
    completed_raw_registry, completed_raw_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=root
            / completed_status["raw_evidence_registry"]["path"],
            workspace_root=root,
        )
    )
    if (
        completed_raw_registry_artifact
        != completed_status["raw_evidence_registry"]
        or completed_raw_registry_artifact
        != final["campaign"]["raw_evidence_registry"]
    ):
        raise ValidationError(
            "candidate final report raw evidence differs from terminal campaign state"
        )
    (
        ledger_entry_count,
        ledger_head_sha256,
        ledger_sha256,
        _,
        ledger_identities,
    ) = _validate_candidate_status_ledger(
        plan=plan,
        status=completed_status,
        ledger_paths=ledger_physical,
        workspace_root=root,
    )
    plan_sha256 = sha256_json(plan)
    final_sha256 = sha256_json(final)
    final_physical_sha256 = sha256_file(final_physical)
    status_by_id = {
        item["task_id"]: item for item in completed_status["tasks"]
    }
    final_task = status_by_id["final"]
    prefix_count = final["campaign"]["status_ledger_entry_count"]
    prefix = _require_candidate_ledger_prefix(
        binding=final["campaign"],
        ledger_identities=ledger_identities,
        label="candidate final report",
    )
    if (
        completed_status["campaign_id"] != plan["campaign_id"]
        or completed_status["plan_sha256"] != plan_sha256
        or completed_status["state"] != "completed"
        or completed_status["ready_tasks"]
        or final_task["status"] != "completed"
        or final_task["evidence_kind"] != "candidate-final.v1"
        or final_task["evidence_sha256"] != final_sha256
        or final_task["evidence_physical_sha256"] != final_physical_sha256
        or final["campaign"]["plan_sha256"] != plan_sha256
        or final["freeze"]["campaign_id"] != plan["campaign_id"]
        or final["freeze"]["run_namespace"] != plan["run_namespace"]
        or ledger_entry_count != prefix_count + 1
        or completed_status["previous_status_sha256"]
        != prefix[-1]["canonical_sha256"]
        or ledger_identities[-1]["canonical_sha256"]
        != sha256_json(completed_status)
        or ledger_identities[-1]["physical_sha256"]
        != sha256_file(status_physical)
    ):
        raise ValidationError(
            "candidate final report requires the exact terminal final/ledger closure"
        )
    run_paths = {
        item["task_id"]: root / item["run_record"]["path"]
        for item in completed_raw_registry["runs"]
    }
    b2_study_path = root / final["studies"]["B2"]["path"]
    main_study_paths = {
        role: root / study["path"]
        for role in ("B3", "B4", "B5", "B6")
        if (study := final["studies"][role]) is not None
    }
    diagnostic_study = final["diagnostics"]["study"]
    expected_final = build_candidate_final(
        screening_path=root / plan["artifacts"]["screening"]["path"],
        catalog_path=root / plan["artifacts"]["candidate_registry"]["path"],
        matrix_path=root / plan["artifacts"]["matrix"]["path"],
        workspace_root=root,
        campaign_plan_path=plan_physical,
        campaign_status_path=ledger_physical[prefix_count - 1],
        status_ledger_paths=ledger_physical[:prefix_count],
        run_paths=run_paths,
        b2_study_path=b2_study_path,
        study_paths=main_study_paths,
        diagnostic_study_path=(
            None if diagnostic_study is None else root / diagnostic_study["path"]
        ),
        freeze_path=root / final["freeze"]["artifact"]["path"],
        final_id=final["final_id"],
    )
    if expected_final != final:
        raise ValidationError(
            "candidate final report content differs from the exact replayed campaign"
        )
    return {
        "campaign_id": plan["campaign_id"],
        "plan_sha256": plan_sha256,
        "candidate_final_sha256": final_sha256,
        "candidate_final_physical_sha256": final_physical_sha256,
        "completed_status_sha256": ledger_head_sha256,
        "completed_status_physical_sha256": sha256_file(status_physical),
        "status_ledger_entry_count": ledger_entry_count,
        "status_ledger_head_sha256": ledger_head_sha256,
        "status_ledger_sha256": ledger_sha256,
        "raw_evidence_registry_sha256": completed_raw_registry_artifact[
            "canonical_sha256"
        ],
        "raw_evidence_registry_physical_sha256": completed_raw_registry_artifact[
            "physical_sha256"
        ],
    }


def finalize_candidate_campaign(
    *,
    plan_path: Path,
    status_path: Path,
    status_ledger_paths: Sequence[Path],
    study_path: Path,
    catalog_path: Path,
    pass_registry_path: Path,
    matrix_path: Path,
    screening_path: Path,
    oracle_capture_path: Path,
    suite_paths: Mapping[str, Path],
    measurement_protocol_path: Path,
    hotblock_measurement_protocol_path: Path,
    reference_toolchain_path: Path,
    compiler_artifact_path: Path,
    workspace_root: Path,
    freeze_id: str,
) -> dict[str, Any]:
    """Seal the completed B2 campaign into the immutable pre-B3 freeze."""

    plan = _load_version(
        plan_path, "candidate-campaign-plan.v1", label="candidate campaign plan"
    )
    status = _load_version(
        status_path, "candidate-campaign-status.v1", label="candidate campaign status"
    )
    study = _load_version(
        study_path, "candidate-study.v1", label="candidate B2 study"
    )
    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    pass_registry = _load_version(
        pass_registry_path, "pass-registry.v2", label="pass registry"
    )
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate matrix"
    )
    plan_artifacts = plan["artifacts"]
    if (
        _frozen_artifact_digest(
            workspace_root, catalog_path, catalog, label="candidate registry"
        ) != plan_artifacts["candidate_registry"]
        or _frozen_artifact_digest(
            workspace_root,
            pass_registry_path,
            pass_registry,
            label="pass registry",
        ) != plan_artifacts["executable_pass_registry"]
        or _frozen_artifact_digest(
            workspace_root, matrix_path, matrix, label="candidate matrix"
        ) != plan_artifacts["matrix"]
        or matrix["candidate_registry_sha256"] != sha256_json(catalog)
        or matrix["pass_registry_sha256"] != sha256_json(pass_registry)
    ):
        raise ValidationError(
            "candidate freeze registry/matrix inputs differ from the B2 plan"
        )
    matrix_profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    screening = _load_and_reverify_candidate_screening(
        screening_path=screening_path,
        workspace_root=workspace_root,
    )
    base_pass_registry = _require_executable_registry_bridge(
        screening=screening,
        catalog=catalog,
        executable_registry=pass_registry,
        workspace_root=workspace_root,
    )
    if _frozen_artifact_digest(
        workspace_root,
        screening_path,
        screening,
        label="candidate screening",
    ) != plan_artifacts["screening"] or screening["base_pass_registry"] != (
        plan_artifacts["screening_base_pass_registry"]
    ):
        raise ValidationError("candidate freeze screening differs from the campaign plan")
    oracle_capture = _load_and_reverify_candidate_oracle_capture(
        capture_path=oracle_capture_path,
        workspace_root=workspace_root,
    )
    _require_screening_capture_binding(screening, oracle_capture)
    plan_digest = sha256_json(plan)
    study_digest = sha256_json(study)
    (
        ledger_entry_count,
        ledger_head_sha256,
        ledger_sha256,
        _,
        _,
    ) = _validate_candidate_status_ledger(
        plan=plan,
        status=status,
        ledger_paths=status_ledger_paths,
        workspace_root=workspace_root,
    )
    _, raw_evidence_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=_candidate_workspace_root(workspace_root)
            / status["raw_evidence_registry"]["path"],
            workspace_root=workspace_root,
        )
    )
    if raw_evidence_registry_artifact != status["raw_evidence_registry"]:
        raise ValidationError(
            "candidate freeze raw-evidence registry differs from pre-B3 status"
        )
    status_by_id = {item["task_id"]: item for item in status["tasks"]}
    if (
        status["campaign_id"] != plan["campaign_id"]
        or status["plan_sha256"] != plan_digest
        or status_by_id["freeze"]["status"] != "pending"
        or status["ready_tasks"] != ["freeze"]
    ):
        raise ValidationError("only the exact dependency-ready pre-B3 task may freeze")
    if (
        status_by_id["study.B2"]["evidence_sha256"] != study_digest
        or study["study_id"] != plan["study_ids"]["B2"]
        or study["data_role"] != "B2"
    ):
        raise ValidationError("candidate freeze B2 study binding differs")
    if set(suite_paths) != set(_CANDIDATE_SUITE_CASE_COUNTS):
        raise ConfigurationError("candidate freeze requires exactly B1 through B6 manifests")
    suites: list[dict[str, Any]] = []
    suite_by_role: dict[str, dict[str, Any]] = {}
    for role, expected_count in _CANDIDATE_SUITE_CASE_COUNTS.items():
        manifest = _load_version(
            suite_paths[role],
            "benchmark-manifest.v1",
            label=f"candidate {role} manifest",
        )
        if (
            manifest["provenance"]["data_role"] != role
            or len(manifest["cases"]) != expected_count
            or {case["target"] for case in manifest["cases"]} != {"rv64gc"}
        ):
            raise ValidationError(
                f"candidate freeze {role} manifest must contain exactly {expected_count} RV64GC cases"
            )
        require_formal_suite_contract(
            role=role,
            manifest=manifest,
            manifest_path=suite_paths[role],
        )
        row = {
            "data_role": role,
            "suite_id": manifest["suite_id"],
            "manifest": _frozen_artifact_digest(
                workspace_root,
                suite_paths[role],
                manifest,
                label=f"candidate {role} manifest",
            ),
            "case_count": expected_count,
        }
        suites.append(row)
        suite_by_role[role] = row
    b2_suite = suite_by_role["B2"]
    if (
        study["suite_id"] != b2_suite["suite_id"]
        or study["manifest_sha256"]
        != b2_suite["manifest"]["canonical_sha256"]
        or next(item for item in plan["suites"] if item["data_role"] == "B2")
        != b2_suite
    ):
        raise ValidationError("candidate freeze B2 manifest differs from plan/study")
    qualified_ids = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    assert all(item is not None for item in qualified_ids)
    frozen_candidate_ids = plan["qualified_candidate_ids"]
    b1_passed_ids = [
        candidate_id
        for candidate_id in frozen_candidate_ids
        if status_by_id[f"run.B1.{candidate_id}"]["status"] == "completed"
    ]
    b1_failed_ids = [
        candidate_id
        for candidate_id in frozen_candidate_ids
        if status_by_id[f"run.B1.{candidate_id}"]["status"]
        in {"failed", "interrupted"}
    ]
    if (
        frozen_candidate_ids != qualified_ids
        or set(b1_passed_ids).isdisjoint(b1_failed_ids) is False
        or set(b1_passed_ids) | set(b1_failed_ids) != set(frozen_candidate_ids)
        or [item["candidate_id"] for item in study["candidates"]]
        != b1_passed_ids
        or any(not item["eligible_for_ranking"] for item in study["candidates"])
        or status_by_id["run.B1.full"]["status"] != "completed"
    ):
        raise ValidationError(
            "candidate freeze B1/B2 selection differs from completed-correct candidates"
        )
    planned_candidate_ids = [
        task["candidate_ids"][0]
        for task in plan["tasks"]
        if task["kind"] == "single" and task["data_role"] == "B2"
    ]
    if planned_candidate_ids != frozen_candidate_ids:
        raise ValidationError("candidate freeze B2 plan differs from frozen candidates")
    if oracle_capture["pair_count"] != 99:
        raise ValidationError("candidate freeze requires the complete 99-pair Oracle capture")
    environment = build_campaign_environment_contract(
        measurement_protocol_path=measurement_protocol_path,
        hotblock_measurement_protocol_path=hotblock_measurement_protocol_path,
        reference_toolchain_path=reference_toolchain_path,
        workspace_root=workspace_root,
    )
    standard_protocol = environment["measurement_protocols"]["standard_proxy"]
    standard_protocol_document = _load_version(
        measurement_protocol_path,
        "measurement-protocol.v1",
        label="standard measurement protocol",
    )
    if (
        plan["measurement_protocol"]["protocol_id"]
        != standard_protocol["protocol_id"]
        or plan["measurement_protocol"]["protocol_sha256"]
        != standard_protocol["protocol_sha256"]
        or plan["measurement_protocol"]["artifact"]
        != _frozen_artifact_digest(
            workspace_root,
            measurement_protocol_path,
            standard_protocol_document,
            label="standard measurement protocol",
        )
    ):
        raise ValidationError("candidate freeze standard protocol differs from plan")
    binding = study["bindings"]
    if (
        binding["repo_dirty"]
        or binding["tracked_diff_sha256"] is not None
        or binding["measurement_protocol_id"] != standard_protocol["protocol_id"]
        or binding["measurement_protocol_sha256"]
        != standard_protocol["protocol_sha256"]
    ):
        raise ValidationError(
            "candidate freeze B2 study is not clean standard-proxy evidence"
        )
    _, repository_tree = _clean_repository_identity(
        workspace_root, binding["repo_commit"]
    )
    compiler_artifact = _frozen_compiler_artifact(
        workspace_root, compiler_artifact_path
    )
    if compiler_artifact["physical_sha256"] != binding["compiler_artifact_sha256"]:
        raise ValidationError(
            "candidate freeze compiler artifact bytes differ from B2 provenance"
        )
    if (
        plan["repository"]["repo_commit"] != binding["repo_commit"]
        or plan["repository"]["repo_tree"] != repository_tree
        or plan["repository"]["compiler_artifact"] != compiler_artifact
    ):
        raise ValidationError("candidate freeze repository/artifact differs from plan")
    base_profile = plan["base_pipeline_profile"]
    if (
        base_profile["profile_id"] != "candidate-empty"
        or {
            "profile_id": base_profile["profile_id"],
            "profile_sha256": base_profile["profile_sha256"],
        }
        != matrix["base_pipeline_profile"]
    ):
        raise ValidationError("candidate freeze base profile is not exact FULL")
    base_profile_record = matrix_profiles["candidate-empty"]
    base_profile_path = (
        _candidate_workspace_root(workspace_root) / base_profile_record["path"]
    )
    base_profile_document = validate_pipeline_profile_v2(
        read_json(base_profile_path),
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    base_profile_artifact = _frozen_artifact_digest(
        workspace_root,
        base_profile_path,
        base_profile_document,
        label="candidate-empty profile",
    )
    if (
        base_profile_artifact["physical_sha256"] != base_profile["profile_sha256"]
        or base_profile_artifact["physical_sha256"] != base_profile["physical_sha256"]
        or base_profile_artifact["path"] != base_profile["path"]
    ):
        raise ValidationError("candidate freeze base profile physical hash differs")

    protocol_documents = {
        "standard_proxy": standard_protocol_document,
        "cache_hotblock": _load_version(
            hotblock_measurement_protocol_path,
            "measurement-protocol.v1",
            label="cache-hotblock measurement protocol",
        ),
    }
    protocol_paths = {
        "standard_proxy": measurement_protocol_path,
        "cache_hotblock": hotblock_measurement_protocol_path,
    }
    frozen_protocols: dict[str, dict[str, Any]] = {}
    for mode in ("standard_proxy", "cache_hotblock"):
        contract = dict(environment["measurement_protocols"][mode])
        artifact = _frozen_artifact_digest(
            workspace_root,
            protocol_paths[mode],
            protocol_documents[mode],
            label=f"candidate {mode} protocol",
        )
        if artifact["canonical_sha256"] != contract["protocol_sha256"]:
            raise ValidationError(
                f"candidate {mode} protocol canonical hash differs"
            )
        contract["path"] = artifact["path"]
        contract["physical_sha256"] = artifact["physical_sha256"]
        frozen_protocols[mode] = contract

    toolchain_document = read_json(reference_toolchain_path)
    if not isinstance(toolchain_document, dict):
        raise ValidationError("candidate reference toolchain must be a JSON object")
    toolchain_contract = dict(environment["reference_toolchain"])
    expected_toolchain_physical = toolchain_contract.pop("snapshot_sha256")
    toolchain_artifact = _frozen_artifact_digest(
        workspace_root,
        reference_toolchain_path,
        toolchain_document,
        label="candidate reference toolchain",
    )
    if toolchain_artifact["physical_sha256"] != expected_toolchain_physical:
        raise ValidationError("candidate reference toolchain physical hash differs")
    toolchain_contract["snapshot"] = toolchain_artifact

    schema_paths = {
        "run_record_schema": workspace_root
        / "tools/benchmark/schemas/run-record.v1.json",
        "candidate_study_schema": workspace_root
        / "tools/benchmark/schemas/candidate-study.v1.json",
    }
    schema_documents = {
        key: read_json(path) for key, path in schema_paths.items()
    }
    frozen_snapshots = {
        "candidate_registry": _frozen_artifact_digest(
            workspace_root, catalog_path, catalog, label="candidate registry"
        ),
        "executable_pass_registry": _frozen_artifact_digest(
            workspace_root,
            pass_registry_path,
            pass_registry,
            label="candidate executable PassRegistry v2",
        ),
        "screening_base_pass_registry": _frozen_artifact_digest(
            workspace_root,
            workspace_root / screening["base_pass_registry"]["path"],
            base_pass_registry,
            label="candidate screening base PassRegistry v2",
        ),
        "matrix": _frozen_artifact_digest(
            workspace_root, matrix_path, matrix, label="candidate matrix"
        ),
        "screening": _frozen_artifact_digest(
            workspace_root, screening_path, screening, label="candidate screening"
        ),
        "oracle_capture": _frozen_artifact_digest(
            workspace_root,
            oracle_capture_path,
            oracle_capture,
            label="candidate Oracle capture",
        ),
        "run_record_schema": _frozen_artifact_digest(
            workspace_root,
            schema_paths["run_record_schema"],
            schema_documents["run_record_schema"],
            label="run-record schema",
        ),
        "candidate_study_schema": _frozen_artifact_digest(
            workspace_root,
            schema_paths["candidate_study_schema"],
            schema_documents["candidate_study_schema"],
            label="candidate-study schema",
        ),
        "candidate_evidence_sha256": screening["candidate_evidence_sha256"],
        "screening_spec_sha256": screening["screening_spec_sha256"],
        "oracle_plan_sha256": oracle_capture["oracle_plan_sha256"],
    }
    run_namespace = plan["run_namespace"]
    if any(
        not task["run_id"].startswith(run_namespace)
        for task in plan["tasks"]
        if task["run_id"] is not None
    ):
        raise ValidationError("candidate freeze plan run id escapes its namespace")
    return validate_document(
        {
            "schema_version": "candidate-freeze.v1",
            "freeze_id": freeze_id,
            "frozen_at": status["as_of"],
            "campaign_id": plan["campaign_id"],
            "run_namespace": run_namespace,
            "repository": {
                "repo_commit": binding["repo_commit"],
                "repo_tree": repository_tree,
                "repo_dirty": False,
                "tracked_diff_sha256": None,
                "compiler_artifact": compiler_artifact,
            },
            "snapshots": frozen_snapshots,
            "base_pipeline_profile": {
                "profile_id": "candidate-empty",
                "artifact": base_profile_artifact,
            },
            "suites": suites,
            "measurement_protocols": frozen_protocols,
            "reference_toolchain": toolchain_contract,
            "gates": {
                "oracle_structure_geometric_mean_minimum": 1.10,
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
            "frozen_candidate_ids": frozen_candidate_ids,
            "frozen_candidate_ids_sha256": sha256_json(frozen_candidate_ids),
            "b2_campaign": {
                "plan_sha256": plan_digest,
                "status_sha256": sha256_json(status),
                "status_ledger_entry_count": ledger_entry_count,
                "status_ledger_head_sha256": ledger_head_sha256,
                "status_ledger_sha256": ledger_sha256,
                "raw_evidence_registry": raw_evidence_registry_artifact,
                "study_id": study["study_id"],
                "study_sha256": study_digest,
                "b1_full_run_id": next(
                    task["run_id"]
                    for task in plan["tasks"]
                    if task["task_id"] == "run.B1.full"
                ),
                "b1_full_run_sha256": status_by_id["run.B1.full"][
                    "evidence_sha256"
                ],
                "b1_passed_candidate_ids": b1_passed_ids,
                "b1_failed_candidate_ids": b1_failed_ids,
            },
        }
    )


def build_candidate_final(
    *,
    screening_path: Path,
    catalog_path: Path,
    matrix_path: Path,
    workspace_root: Path,
    campaign_plan_path: Path,
    campaign_status_path: Path,
    status_ledger_paths: Sequence[Path],
    run_paths: Mapping[str, Path],
    b2_study_path: Path,
    study_paths: Mapping[str, Path],
    diagnostic_study_path: Path | None,
    freeze_path: Path,
    final_id: str,
) -> dict[str, Any]:
    """Build the B3/B4/B5/B6 final using equal weight for all 267 cases."""

    if "B3" not in study_paths or not set(study_paths).issubset({"B3", "B4", "B5", "B6"}):
        raise ConfigurationError("candidate final requires B3 and only B3-B6 studies")
    plan = _load_version(
        campaign_plan_path,
        "candidate-campaign-plan.v1",
        label="candidate full-stage campaign plan",
    )
    campaign_status = _load_version(
        campaign_status_path,
        "candidate-campaign-status.v1",
        label="candidate full-stage campaign status",
    )
    plan_sha256 = sha256_json(plan)
    (
        ledger_entry_count,
        ledger_head_sha256,
        ledger_sha256,
        _,
        ledger_identities,
    ) = _validate_candidate_status_ledger(
        plan=plan,
        status=campaign_status,
        ledger_paths=status_ledger_paths,
        workspace_root=workspace_root,
    )
    status_by_id = {item["task_id"]: item for item in campaign_status["tasks"]}
    if (
        campaign_status["campaign_id"] != plan["campaign_id"]
        or campaign_status["plan_sha256"] != plan_sha256
        or campaign_status["ready_tasks"] != ["final"]
        or status_by_id["final"]["status"] != "pending"
    ):
        raise ValidationError("candidate final requires the exact terminal full-stage status")
    raw_evidence_registry, raw_evidence_registry_artifact = (
        _load_and_reverify_candidate_raw_evidence_registry(
            plan=plan,
            registry_path=_candidate_workspace_root(workspace_root)
            / campaign_status["raw_evidence_registry"]["path"],
            workspace_root=workspace_root,
            expected_run_paths=run_paths,
        )
    )
    if raw_evidence_registry_artifact != campaign_status["raw_evidence_registry"]:
        raise ValidationError(
            "candidate final raw-evidence registry differs from pre-final status"
        )
    raw_verifications = _raw_verifications_by_run_id(raw_evidence_registry)
    screening = _load_and_reverify_candidate_screening(
        screening_path=screening_path,
        workspace_root=workspace_root,
    )
    catalog = _load_version(
        catalog_path, "candidate-catalog.v1", label="candidate registry"
    )
    matrix = _load_version(
        matrix_path, "candidate-profile-matrix.v1", label="candidate profile matrix"
    )
    freeze = _load_version(
        freeze_path, "candidate-freeze.v1", label="candidate pre-B3 freeze"
    )
    frozen_documents = _verify_candidate_freeze_inputs(
        freeze,
        workspace_root=workspace_root,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    _require_candidate_ledger_prefix(
        binding=freeze["b2_campaign"],
        ledger_identities=ledger_identities,
        label="candidate final freeze",
    )
    registry_sha256 = sha256_json(catalog)
    if matrix["candidate_registry_sha256"] != registry_sha256:
        raise ValidationError("candidate final matrix/registry binding differs")
    if (
        _require_executable_registry_bridge(
            screening=screening,
            catalog=catalog,
            executable_registry=frozen_documents["executable_pass_registry"],
            workspace_root=workspace_root,
        )
        != frozen_documents["screening_base_pass_registry"]
        or screening["pass_registry_sha256"]
        != sha256_json(frozen_documents["screening_base_pass_registry"])
    ):
        raise ValidationError("candidate final screening/PassRegistry binding differs")
    profiles = _verify_matrix_profiles(
        workspace_root,
        matrix,
        candidate_order=[item["candidate_id"] for item in catalog["candidates"]],
    )
    qualified_ids = [
        item["implementation_candidate_id"]
        for item in screening["candidates"]
        if item["qualification_status"] == "qualified"
    ]
    assert all(item is not None for item in qualified_ids)
    catalog_ids = [item["candidate_id"] for item in catalog["candidates"]]
    if catalog_ids != qualified_ids:
        raise ValidationError(
            "executable candidate catalog must contain the qualified screening candidates in order"
        )
    if plan["qualified_candidate_ids"] != catalog_ids:
        raise ValidationError("candidate final campaign candidate order differs")
    diagnostic_status = campaign_status["diagnostic_plan"]
    if diagnostic_status is None or any(
        item["status"] not in {"completed", "failed", "interrupted"}
        for item in diagnostic_status["tasks"]
    ):
        raise ValidationError(
            "candidate final requires terminal freeze-bound post-B3 diagnostics"
        )
    diagnostic_study_status = diagnostic_status["study"]
    if diagnostic_study_status["status"] not in {"completed", "ineligible"}:
        raise ValidationError("candidate final requires terminal diagnostic study state")
    if (diagnostic_study_path is not None) != (
        diagnostic_study_status["status"] == "completed"
    ):
        raise ValidationError(
            "candidate final diagnostic study path/status binding differs"
        )
    diagnostic_matrix = _load_frozen_artifact(
        workspace_root,
        diagnostic_status["matrix"],
        label="candidate final diagnostic matrix",
        version="candidate-profile-matrix.v1",
    )
    if (
        diagnostic_matrix["candidate_registry_sha256"] != registry_sha256
        or diagnostic_matrix["pass_registry_sha256"]
        != sha256_json(frozen_documents["executable_pass_registry"])
    ):
        raise ValidationError("candidate final diagnostic matrix registry binding differs")
    diagnostic_profiles = _verify_matrix_profiles(
        workspace_root,
        diagnostic_matrix,
        candidate_order=catalog_ids,
    )
    diagnostic_tasks = {
        item["task_id"]: item for item in diagnostic_status["tasks"]
    }
    run_statuses = {
        task_id: row
        for task_id, row in status_by_id.items()
        if row["evidence_kind"] == "run-record.v1"
    }
    run_statuses.update(
        {
            task_id: row
            for task_id, row in diagnostic_tasks.items()
            if row["evidence_sha256"] is not None
        }
    )
    if set(run_paths) != set(run_statuses):
        raise ValidationError(
            "candidate final requires every and only hash-bound full-stage raw run"
        )
    plan_tasks = {item["task_id"]: item for item in plan["tasks"]}
    raw_runs: dict[str, dict[str, Any]] = {}
    for task_id, path in run_paths.items():
        run = _load_version(path, "run-record.v1", label=f"candidate final raw run {task_id}")
        if (
            sha256_json(run) != run_statuses[task_id]["evidence_sha256"]
            or sha256_file(path)
            != run_statuses[task_id]["evidence_physical_sha256"]
        ):
            raise ValidationError(f"candidate final raw run/status hash differs: {task_id}")
        if task_id in plan_tasks:
            task = plan_tasks[task_id]
            if task["task_type"] != "run":
                raise ValidationError(f"candidate final task is not a run: {task_id}")
            _validate_campaign_run(plan, task, run, profiles=profiles)
            if task["kind"] == "reference":
                _require_frozen_reference_run(run, task, freeze)
            binding = _binding_record(run)
            if (
                binding["repo_commit"] != plan["repository"]["repo_commit"]
                or binding["repo_dirty"]
                or binding["tracked_diff_sha256"] is not None
                or binding["measurement_protocol_id"]
                != plan["measurement_protocol"]["protocol_id"]
                or binding["measurement_protocol_sha256"]
                != plan["measurement_protocol"]["protocol_sha256"]
                or (
                    task["kind"] != "reference"
                    and binding["compiler_artifact_sha256"]
                    != plan["repository"]["compiler_artifact"]["physical_sha256"]
                )
            ):
                raise ValidationError(
                    f"candidate final raw run differs from campaign freeze: {task_id}"
                )
        else:
            task = diagnostic_tasks[task_id]
            _validate_candidate_diagnostic_run(
                task=task,
                run=run,
                profile=diagnostic_profiles[task["logical_profile_id"]],
                candidate_registry_sha256=registry_sha256,
                pass_registry_sha256=sha256_json(
                    frozen_documents["executable_pass_registry"]
                ),
                b3_suite=next(
                    item for item in plan["suites"] if item["data_role"] == "B3"
                ),
                freeze=freeze,
            )
        raw_runs[task_id] = run
    freeze_snapshots = freeze["snapshots"]
    if (
        status_by_id["freeze"]["status"] != "completed"
        or status_by_id["freeze"]["evidence_sha256"] != sha256_json(freeze)
        or status_by_id["freeze"]["evidence_physical_sha256"]
        != sha256_file(freeze_path)
        or frozen_documents["candidate_registry"] != catalog
        or frozen_documents["matrix"] != matrix
        or frozen_documents["screening"] != screening
        or sha256_json(frozen_documents["executable_pass_registry"])
        != catalog["pass_registry_sha256"]
        or _frozen_artifact_digest(
            workspace_root,
            catalog_path,
            catalog,
            label="candidate registry",
        )
        != freeze_snapshots["candidate_registry"]
        or _frozen_artifact_digest(
            workspace_root,
            matrix_path,
            matrix,
            label="candidate matrix",
        )
        != freeze_snapshots["matrix"]
        or _frozen_artifact_digest(
            workspace_root,
            screening_path,
            screening,
            label="candidate screening",
        )
        != freeze_snapshots["screening"]
        or freeze["frozen_candidate_ids"] != catalog_ids
        or freeze["base_pipeline_profile"]["profile_id"]
        != matrix["base_pipeline_profile"]["profile_id"]
        or freeze["base_pipeline_profile"]["artifact"]["physical_sha256"]
        != matrix["base_pipeline_profile"]["profile_sha256"]
        or plan["artifacts"]["candidate_registry"]
        != freeze_snapshots["candidate_registry"]
        or plan["artifacts"]["executable_pass_registry"]
        != freeze_snapshots["executable_pass_registry"]
        or plan["artifacts"]["screening_base_pass_registry"]
        != freeze_snapshots["screening_base_pass_registry"]
        or plan["artifacts"]["matrix"] != freeze_snapshots["matrix"]
        or plan["artifacts"]["screening"] != freeze_snapshots["screening"]
        or plan["repository"] != {
            "repo_commit": freeze["repository"]["repo_commit"],
            "repo_tree": freeze["repository"]["repo_tree"],
            "compiler_artifact": freeze["repository"]["compiler_artifact"],
        }
        or plan["measurement_protocol"]["protocol_id"]
        != freeze["measurement_protocols"]["standard_proxy"]["protocol_id"]
        or plan["measurement_protocol"]["protocol_sha256"]
        != freeze["measurement_protocols"]["standard_proxy"]["protocol_sha256"]
    ):
        raise ValidationError(
            "candidate final inputs differ from the immutable pre-B3 freeze"
        )
    b1_full_run = raw_runs.get("run.B1.full")
    if b1_full_run is None or (
        b1_full_run["state"] != "completed"
        or sha256_json(b1_full_run) != freeze["b2_campaign"]["b1_full_run_sha256"]
        or b1_full_run["run_id"] != freeze["b2_campaign"]["b1_full_run_id"]
    ):
        raise ValidationError("candidate final lacks the exact completed B1 FULL baseline")
    b1_run_paths = {
        candidate_id: run_paths[f"run.B1.{candidate_id}"]
        for candidate_id in catalog_ids
    }

    studies: dict[str, dict[str, Any]] = {}
    results_by_role: dict[str, dict[str, Mapping[str, Any]]] = {}
    common_binding: Mapping[str, Any] | None = None
    common_keys = (
        "repo_commit",
        "repo_dirty",
        "tracked_diff_sha256",
        "compiler_artifact_sha256",
        "measurement_protocol_id",
        "measurement_protocol_sha256",
    )
    all_study_paths = {"B2": b2_study_path, **study_paths}
    frozen_suites = {
        item["data_role"]: item for item in freeze["suites"]
    }
    for role in ("B2", "B3", "B4", "B5", "B6"):
        if role not in all_study_paths:
            results_by_role[role] = {}
            continue
        study = _load_version(
            all_study_paths[role], "candidate-study.v1", label=f"{role} candidate study"
        )
        if (
            study["data_role"] != role
            or study["candidate_registry_sha256"] != registry_sha256
            or study["matrix_sha256"] != sha256_json(matrix)
            or study["pass_registry_sha256"] != catalog["pass_registry_sha256"]
            or study["suite_id"] != frozen_suites[role]["suite_id"]
            or study["manifest_sha256"]
            != frozen_suites[role]["manifest"]["canonical_sha256"]
        ):
            raise ValidationError(f"{role} candidate study binding differs")
        if common_binding is None:
            common_binding = study["bindings"]
        elif any(study["bindings"][key] != common_binding[key] for key in common_keys):
            raise ValidationError(f"{role} candidate study artifact/commit/protocol differs")
        by_id = {item["candidate_id"]: item for item in study["candidates"]}
        if not set(by_id).issubset(set(catalog_ids)):
            raise ValidationError(f"{role} candidate study contains an unqualified candidate")
        _validate_study_against_raw_runs(
            study=study,
            baseline=raw_runs[f"run.{role}.full"],
            candidate_runs={
                candidate_id: raw_runs[f"run.{role}.{candidate_id}"]
                for candidate_id in by_id
            },
            interaction_runs={},
            catalog=catalog,
            raw_verifications=raw_verifications,
        )
        if not study["baseline"]["run_id"].startswith(
            freeze["run_namespace"]
        ) or any(
            not item["run_id"].startswith(freeze["run_namespace"])
            for item in [*study["candidates"], *study["interactions"]]
        ):
            raise ValidationError(
                f"{role} candidate study run id escapes the frozen namespace"
            )
        studies[role] = study
        results_by_role[role] = by_id
        study_status = status_by_id[f"study.{role}"]
        if (
            study_status["status"] != "completed"
            or study_status["evidence_sha256"] != sha256_json(study)
            or study_status["evidence_physical_sha256"]
            != sha256_file(all_study_paths[role])
        ):
            raise ValidationError(f"candidate final {role} study/status hash differs")

    if sha256_json(studies["B2"]) != freeze["b2_campaign"]["study_sha256"]:
        raise ValidationError("candidate final B2 study differs from pre-B3 freeze")
    diagnostic_study: dict[str, Any] | None = None
    if diagnostic_study_path is not None:
        diagnostic_study = _load_version(
            diagnostic_study_path,
            "candidate-study.v1",
            label="candidate final diagnostic interaction study",
        )
        if (
            diagnostic_study_status["evidence_kind"] != "candidate-study.v1"
            or diagnostic_study_status["evidence_sha256"]
            != sha256_json(diagnostic_study)
            or diagnostic_study_status["evidence_physical_sha256"]
            != sha256_file(diagnostic_study_path)
        ):
            raise ValidationError(
                "candidate final diagnostic study/status hash differs"
            )
        _validate_candidate_diagnostic_study(
            study=diagnostic_study,
            formal_b3_study=studies["B3"],
            baseline=raw_runs["run.B3.full"],
            candidate_runs={
                candidate_id: raw_runs[f"run.B3.{candidate_id}"]
                for candidate_id in diagnostic_status["top3_candidate_ids"]
            },
            pair_runs={
                frozenset(task["candidate_ids"]): (
                    task,
                    raw_runs[task["task_id"]],
                )
                for task in diagnostic_status["tasks"]
                if task["kind"] == "pair"
            },
            top3_candidate_ids=diagnostic_status["top3_candidate_ids"],
            matrix_sha256=sha256_json(diagnostic_matrix),
            catalog=catalog,
            raw_verifications=raw_verifications,
        )
    assert common_binding is not None
    frozen_repository = freeze["repository"]
    standard_protocol = freeze["measurement_protocols"]["standard_proxy"]
    if (
        common_binding["repo_commit"] != frozen_repository["repo_commit"]
        or common_binding["repo_dirty"]
        or common_binding["tracked_diff_sha256"] is not None
        or common_binding["compiler_artifact_sha256"]
        != frozen_repository["compiler_artifact"]["physical_sha256"]
        or common_binding["measurement_protocol_id"]
        != standard_protocol["protocol_id"]
        or common_binding["measurement_protocol_sha256"]
        != standard_protocol["protocol_sha256"]
    ):
        raise ValidationError(
            "candidate final studies differ from frozen code/artifact/protocol"
        )

    b1_runs: dict[str, dict[str, Any]] = {}
    b1_passed_ids: set[str] = set()
    b1_run_ids: set[str] = set()
    b1_run_hashes: set[str] = set()
    for candidate_id in catalog_ids:
        run = _load_version(
            b1_run_paths[candidate_id],
            "run-record.v1",
            label=f"B1 correctness run {candidate_id}",
        )
        run_hash = sha256_json(run)
        if run["run_id"] in b1_run_ids or run_hash in b1_run_hashes:
            raise ValidationError("B1 correctness runs must be physically distinct")
        b1_run_ids.add(run["run_id"])
        b1_run_hashes.add(run_hash)
        if (
            run["state"] not in {"completed", "failed", "interrupted"}
            or run["configuration"]["evidence_level"] != "qemu_correctness"
            or run["suite_id"] != frozen_suites["B1"]["suite_id"]
            or run["manifest_sha256"]
            != frozen_suites["B1"]["manifest"]["canonical_sha256"]
            or not run["run_id"].startswith(freeze["run_namespace"])
        ):
            raise ValidationError(
                f"B1 candidate run is not terminal qemu_correctness evidence: {candidate_id}"
            )
        _require_candidate_run_protocol(run, data_role="B1")
        _require_candidate_correctness_run(
            run, label=f"B1 candidate {candidate_id}"
        )
        binding = _binding_record(run)
        for key in (
            "repo_commit",
            "repo_dirty",
            "tracked_diff_sha256",
            "compiler_artifact_sha256",
        ):
            if binding[key] != common_binding[key]:
                raise ValidationError(
                    f"B1 candidate run differs from the frozen artifact/revision: {candidate_id}"
                )
        profile = next(
            item
            for item in matrix["profiles"]
            if item["kind"] == "single" and item["candidate_ids"] == [candidate_id]
        )
        _require_candidate_configuration(
            run,
            registry_sha256=registry_sha256,
            pass_registry_sha256=catalog["pass_registry_sha256"],
            enabled_candidate_ids=[candidate_id],
            profile=profiles[profile["profile_id"]],
            label=f"B1 candidate {candidate_id}",
        )
        b1_runs[candidate_id] = run
        if run["state"] == "completed":
            b1_passed_ids.add(candidate_id)

    if (
        b1_passed_ids != set(freeze["b2_campaign"]["b1_passed_candidate_ids"])
        or set(catalog_ids) - b1_passed_ids
        != set(freeze["b2_campaign"]["b1_failed_candidate_ids"])
    ):
        raise ValidationError("candidate final B1 pass/fail partition differs from freeze")

    if set(results_by_role["B2"]) != b1_passed_ids:
        raise ValidationError("B2 tuning must contain exactly the B1-correct candidates")
    if set(results_by_role["B3"]) != b1_passed_ids:
        raise ValidationError("B3 must contain exactly the B1-correct candidates")

    b3_results = results_by_role["B3"]
    promoted_ids = {
        candidate_id
        for candidate_id, result in b3_results.items()
        if result["eligible_for_ranking"]
        and result["case_geometric_mean_speedup"] is not None
        and result["case_geometric_mean_speedup"] > 1.0
    }
    expected_validation_roles = {"B4", "B5", "B6"} if promoted_ids else set()
    if set(study_paths) - {"B3"} != expected_validation_roles:
        raise ValidationError(
            "candidate final B4-B6 study presence differs from the B3 promotion gate"
        )
    for role in ("B4", "B5", "B6"):
        if set(results_by_role[role]) != promoted_ids:
            raise ValidationError(
                f"{role} must contain exactly all B3 candidates with GM strictly greater than 1"
            )

    candidate_rows: list[dict[str, Any]] = []
    for screened in screening["candidates"]:
        candidate_id = screened["candidate_id"]
        implementation_id = screened["implementation_candidate_id"]
        outcomes: dict[str, dict[str, Any] | None] = {}
        raw_results: dict[str, Mapping[str, Any] | None] = {}
        for role in ("B3", "B4", "B5", "B6"):
            result = results_by_role[role].get(implementation_id)
            raw_results[role] = result
            outcomes[role] = (
                None
                if result is None
                else {
                    "study_id": studies[role]["study_id"],
                    "run_id": result["run_id"],
                    "run_sha256": result["run_sha256"],
                    "configuration_sha256": result["configuration_sha256"],
                    "suite_id": studies[role]["suite_id"],
                    "manifest_sha256": studies[role]["manifest_sha256"],
                    "expected_case_count": _CANDIDATE_SUITE_CASE_COUNTS[role],
                    "eligible_for_ranking": result["eligible_for_ranking"],
                    "ineligibility_reason": result["ineligibility_reason"],
                    "comparable_cases": result["comparable_cases"],
                    "comparable_source_groups": result[
                        "comparable_source_groups"
                    ],
                    "correctness_failures": result["correctness_failures"],
                    "censored_cases": result["censored_cases"],
                    "excluded_cases": result["excluded_cases"],
                    "case_geometric_mean_speedup": result[
                        "case_geometric_mean_speedup"
                    ],
                    "source_group_geometric_mean_speedup": result[
                        "source_group_geometric_mean_speedup"
                    ],
                    "confidence_interval_95": result["confidence_interval_95"],
                    "static_text_bytes_full": result["static_text_bytes_full"],
                    "static_text_bytes_full_plus_candidate": result[
                        "static_text_bytes_full_plus_candidate"
                    ],
                    "static_text_ratio": result["static_text_ratio"],
                }
            )
        reasons: list[str] = []
        b3 = raw_results["B3"]
        b1_run = b1_runs.get(implementation_id)
        b2_result = results_by_role["B2"].get(implementation_id)
        if screened["qualification_status"] != "qualified":
            reasons.append("screening_not_qualified")
        elif b1_run is None or b1_run["state"] != "completed":
            reasons.append("b1_not_correct")
        elif b3 is None:
            reasons.append("missing_b3_evidence")
        elif not b3["eligible_for_ranking"]:
            reasons.append("suite_ineligible")
        elif b3["case_geometric_mean_speedup"] <= 1.0:
            reasons.append("b3_not_above_one")
        else:
            validation = [raw_results[role] for role in ("B4", "B5", "B6")]
            if any(item is None for item in validation):
                reasons.append("missing_validation_suite")
            elif any(not item["eligible_for_ranking"] for item in validation if item is not None):
                reasons.append("suite_ineligible")

        complete_results = [
            raw_results[role]
            for role in ("B3", "B4", "B5", "B6")
            if raw_results[role] is not None
        ]
        combined_case_count = sum(item["comparable_cases"] for item in complete_results)
        combined_gm: float | None = None
        b3_gm = None if b3 is None else b3["case_geometric_mean_speedup"]
        static_candidate_bytes: float | None = None
        static_ratio: float | None = None
        if len(complete_results) == 4 and all(
            item["eligible_for_ranking"] for item in complete_results
        ):
            per_case_speedups = [
                float(case["speedup"])
                for item in complete_results
                for case in item["per_cases"]
            ]
            if per_case_speedups:
                combined_gm = math.exp(
                    sum(math.log(value) for value in per_case_speedups)
                    / len(per_case_speedups)
                )
            static_complete = all(
                item["static_text_bytes_full"] is not None
                and item["static_text_bytes_full_plus_candidate"] is not None
                for item in complete_results
            )
            if static_complete:
                static_full = sum(
                    float(item["static_text_bytes_full"]) for item in complete_results
                )
                static_candidate = sum(
                    float(item["static_text_bytes_full_plus_candidate"])
                    for item in complete_results
                )
                if static_full > 0 and static_candidate > 0:
                    static_candidate_bytes = static_candidate
                    static_ratio = static_full / static_candidate
        if not reasons and combined_case_count != 267:
            reasons.append("combined_case_count_not_267")
        if not reasons and static_ratio is None:
            reasons.append("missing_static_text_evidence")
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "oracle_family_id": screened["oracle_family_id"],
                "implementation_candidate_id": implementation_id,
                "screening_status": screened["qualification_status"],
                "screening_rejection_reasons": screened["rejection_reasons"],
                "b1_correctness": (
                    None
                    if b1_run is None
                    else {
                        "run_id": b1_run["run_id"],
                        "run_sha256": sha256_json(b1_run),
                        "configuration_sha256": b1_run["configuration_sha256"],
                        "suite_id": b1_run["suite_id"],
                        "manifest_sha256": b1_run["manifest_sha256"],
                        "case_count": b1_run["manifest_case_count"],
                        "evidence_level": b1_run["configuration"]["evidence_level"],
                        "state": b1_run["state"],
                        "passed_cases": b1_run["summary"]["passed_cases"],
                        "failed_cases": b1_run["summary"]["failed_cases"],
                        "pending_cases": b1_run["summary"]["pending_cases"],
                        "censored_cases": b1_run["summary"]["censored_cases"],
                        "all_correct": b1_run["state"] == "completed",
                        "failure_reason": campaign_run_status(b1_run)[1],
                    }
                ),
                "b2_tuning": (
                    None
                    if b2_result is None
                    else {
                        "study_id": studies["B2"]["study_id"],
                        "run_id": b2_result["run_id"],
                        "run_sha256": b2_result["run_sha256"],
                        "configuration_sha256": b2_result[
                            "configuration_sha256"
                        ],
                        "suite_id": studies["B2"]["suite_id"],
                        "manifest_sha256": studies["B2"]["manifest_sha256"],
                        "expected_case_count": _CANDIDATE_SUITE_CASE_COUNTS[
                            "B2"
                        ],
                        "eligible_for_ranking": b2_result["eligible_for_ranking"],
                        "ineligibility_reason": b2_result["ineligibility_reason"],
                        "comparable_cases": b2_result["comparable_cases"],
                        "comparable_source_groups": b2_result[
                            "comparable_source_groups"
                        ],
                        "correctness_failures": b2_result[
                            "correctness_failures"
                        ],
                        "censored_cases": b2_result["censored_cases"],
                        "excluded_cases": b2_result["excluded_cases"],
                        "case_geometric_mean_speedup": b2_result[
                            "case_geometric_mean_speedup"
                        ],
                        "source_group_geometric_mean_speedup": b2_result[
                            "source_group_geometric_mean_speedup"
                        ],
                        "confidence_interval_95": b2_result[
                            "confidence_interval_95"
                        ],
                        "static_text_bytes_full": b2_result["static_text_bytes_full"],
                        "static_text_bytes_full_plus_candidate": b2_result[
                            "static_text_bytes_full_plus_candidate"
                        ],
                        "static_text_ratio": b2_result["static_text_ratio"],
                    }
                ),
                "suite_outcomes": outcomes,
                "eligible_for_final": not reasons,
                "final_ineligibility_reasons": reasons,
                "combined_case_count": combined_case_count,
                "combined_case_geometric_mean_speedup": combined_gm,
                "b3_case_geometric_mean_speedup": b3_gm,
                "combined_static_text_bytes_full_plus_candidate": static_candidate_bytes,
                "combined_static_text_ratio": static_ratio,
                "rank": None,
            }
        )

    eligible = [item for item in candidate_rows if item["eligible_for_final"]]
    eligible.sort(
        key=lambda item: (
            -float(item["combined_case_geometric_mean_speedup"]),
            -float(item["b3_case_geometric_mean_speedup"]),
            float(item["combined_static_text_bytes_full_plus_candidate"]),
            item["implementation_candidate_id"],
        )
    )
    rank_by_id = {
        item["implementation_candidate_id"]: index
        for index, item in enumerate(eligible, 1)
    }
    for item in candidate_rows:
        item["rank"] = rank_by_id.get(item["implementation_candidate_id"])
    ranking = [
        {
            "rank": index,
            "candidate_id": item["implementation_candidate_id"],
            "combined_case_geometric_mean_speedup": item[
                "combined_case_geometric_mean_speedup"
            ],
            "b3_case_geometric_mean_speedup": item[
                "b3_case_geometric_mean_speedup"
            ],
            "combined_static_text_bytes_full_plus_candidate": item[
                "combined_static_text_bytes_full_plus_candidate"
            ],
            "combined_static_text_ratio": item["combined_static_text_ratio"],
            "stable_id_tiebreak": item["implementation_candidate_id"],
        }
        for index, item in enumerate(eligible, 1)
    ]
    winner_candidate_id = (
        ranking[0]["candidate_id"]
        if ranking and ranking[0]["combined_case_geometric_mean_speedup"] > 1.0
        else None
    )
    return validate_document(
        {
            "schema_version": "candidate-final.v1",
            "final_id": final_id,
            "generated_at": campaign_status["as_of"],
            "screening_sha256": sha256_json(screening),
            "candidate_registry_sha256": registry_sha256,
            "executable_pass_registry_sha256": catalog[
                "pass_registry_sha256"
            ],
            "matrix_sha256": sha256_json(matrix),
            "expected_combined_case_count": 267,
            "campaign": {
                "plan_sha256": plan_sha256,
                "status_sha256": sha256_json(campaign_status),
                "status_ledger_entry_count": ledger_entry_count,
                "status_ledger_head_sha256": ledger_head_sha256,
                "status_ledger_sha256": ledger_sha256,
                "raw_evidence_registry": raw_evidence_registry_artifact,
                "run_records": [
                    {
                        "task_id": task_id,
                        "run_id": run["run_id"],
                        "run_sha256": sha256_json(run),
                        "run_physical_sha256": sha256_file(run_paths[task_id]),
                        "state": run["state"],
                    }
                    for task_id in [
                        *[
                            item["task_id"]
                            for item in plan["tasks"]
                            if item["task_id"] in raw_runs
                        ],
                        *[
                            item["task_id"]
                            for item in diagnostic_status["tasks"]
                            if item["task_id"] in raw_runs
                        ],
                    ]
                    for run in [raw_runs[task_id]]
                ],
            },
            "b1_full_correctness": {
                "run_id": b1_full_run["run_id"],
                "run_sha256": sha256_json(b1_full_run),
                "configuration_sha256": b1_full_run["configuration_sha256"],
                "suite_id": b1_full_run["suite_id"],
                "manifest_sha256": b1_full_run["manifest_sha256"],
                "case_count": b1_full_run["manifest_case_count"],
                "evidence_level": b1_full_run["configuration"]["evidence_level"],
                "state": b1_full_run["state"],
                "passed_cases": b1_full_run["summary"]["passed_cases"],
                "failed_cases": b1_full_run["summary"]["failed_cases"],
                "pending_cases": b1_full_run["summary"]["pending_cases"],
                "censored_cases": b1_full_run["summary"]["censored_cases"],
                "all_correct": True,
                "failure_reason": None,
            },
            "freeze": {
                "freeze_id": freeze["freeze_id"],
                "freeze_sha256": sha256_json(freeze),
                "artifact": _frozen_artifact_digest(
                    workspace_root,
                    freeze_path,
                    freeze,
                    label="candidate final freeze",
                ),
                "campaign_id": freeze["campaign_id"],
                "run_namespace": freeze["run_namespace"],
                "repo_commit": freeze["repository"]["repo_commit"],
                "repo_tree": freeze["repository"]["repo_tree"],
                "compiler_artifact": freeze["repository"]["compiler_artifact"],
                "candidate_registry": freeze["snapshots"]["candidate_registry"],
                "executable_pass_registry": freeze["snapshots"][
                    "executable_pass_registry"
                ],
                "screening_base_pass_registry": freeze["snapshots"][
                    "screening_base_pass_registry"
                ],
                "matrix": freeze["snapshots"]["matrix"],
                "screening": freeze["snapshots"]["screening"],
                "oracle_capture": freeze["snapshots"]["oracle_capture"],
                "run_record_schema": freeze["snapshots"]["run_record_schema"],
                "candidate_study_schema": freeze["snapshots"][
                    "candidate_study_schema"
                ],
                "base_pipeline_profile": freeze["base_pipeline_profile"][
                    "artifact"
                ],
                "suite_manifests": {
                    role: frozen_suites[role]["manifest"]
                    for role in _CANDIDATE_SUITE_CASE_COUNTS
                },
                "standard_measurement_protocol": {
                    "path": freeze["measurement_protocols"]["standard_proxy"][
                        "path"
                    ],
                    "canonical_sha256": freeze["measurement_protocols"][
                        "standard_proxy"
                    ]["protocol_sha256"],
                    "physical_sha256": freeze["measurement_protocols"][
                        "standard_proxy"
                    ]["physical_sha256"],
                },
                "hotblock_measurement_protocol": {
                    "path": freeze["measurement_protocols"]["cache_hotblock"][
                        "path"
                    ],
                    "canonical_sha256": freeze["measurement_protocols"][
                        "cache_hotblock"
                    ]["protocol_sha256"],
                    "physical_sha256": freeze["measurement_protocols"][
                        "cache_hotblock"
                    ]["physical_sha256"],
                },
                "reference_toolchain": _candidate_reference_toolchain_context(
                    freeze
                ),
                "raw_evidence_registry": freeze["b2_campaign"][
                    "raw_evidence_registry"
                ],
                "frozen_candidate_ids_sha256": freeze[
                    "frozen_candidate_ids_sha256"
                ],
                "b2_study_sha256": sha256_json(studies["B2"]),
                "freeze_status_ledger_entry_count": freeze["b2_campaign"][
                    "status_ledger_entry_count"
                ],
                "freeze_status_ledger_head_sha256": freeze["b2_campaign"][
                    "status_ledger_head_sha256"
                ],
                "freeze_status_ledger_sha256": freeze["b2_campaign"][
                    "status_ledger_sha256"
                ],
                "b1_full_run_sha256": freeze["b2_campaign"][
                    "b1_full_run_sha256"
                ],
                "b1_passed_candidate_ids": freeze["b2_campaign"][
                    "b1_passed_candidate_ids"
                ],
                "b1_failed_candidate_ids": freeze["b2_campaign"][
                    "b1_failed_candidate_ids"
                ],
                "oracle_threshold": freeze["gates"][
                    "oracle_structure_geometric_mean_minimum"
                ],
                "b3_strict_threshold": freeze["gates"][
                    "b3_geometric_mean_strictly_above"
                ],
                "combined_case_count": freeze["gates"][
                    "combined_case_count"
                ],
                "ranking_rule": [
                    freeze["ranking_rule"]["primary"],
                    freeze["ranking_rule"]["secondary"],
                    freeze["ranking_rule"]["tertiary"],
                    freeze["ranking_rule"]["quaternary"],
                ],
            },
            "studies": {
                role: (
                    None
                    if role not in studies
                    else _candidate_final_study_ref(
                        studies[role],
                        study_path=all_study_paths[role],
                        workspace_root=workspace_root,
                    )
                )
                for role in ("B2", "B3", "B4", "B5", "B6")
            },
            "diagnostics": {
                "source_freeze_sha256": diagnostic_status[
                    "source_freeze_sha256"
                ],
                "source_study_sha256": diagnostic_status[
                    "source_study_sha256"
                ],
                "matrix": diagnostic_status["matrix"],
                "top3_candidate_ids": diagnostic_status[
                    "top3_candidate_ids"
                ],
                "tasks": diagnostic_status["tasks"],
                "study_status": diagnostic_study_status["status"],
                "study_ineligibility_reason": diagnostic_study_status[
                    "ineligibility_reason"
                ],
                "study": (
                    None
                    if diagnostic_study is None
                    else {
                        "study_id": diagnostic_study["study_id"],
                        "path": _workspace_regular_path(
                            workspace_root,
                            diagnostic_study_path,
                            label="candidate final diagnostic study",
                        )[2].as_posix(),
                        "canonical_sha256": sha256_json(diagnostic_study),
                        "physical_sha256": sha256_file(
                            diagnostic_study_path
                        ),
                    }
                ),
            },
            "candidates": candidate_rows,
            "ranking": ranking,
            "winner_candidate_id": winner_candidate_id,
            "winner_reason": (
                "top_combined_gm_above_one"
                if winner_candidate_id is not None
                else "no_winning_candidate"
            ),
        }
    )
