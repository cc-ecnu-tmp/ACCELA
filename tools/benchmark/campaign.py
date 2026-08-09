from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .ablation import _require_formal_measurement
from .errors import ConfigurationError, ValidationError
from .metrics import cache_hotblock_metrics_v1, rv64gc_qemu_v1
from .schema import load_and_validate, validate_document
from .util import safe_slug, sha256_file, sha256_json, utc_now, validate_relative_path


_SUITE_ROLES = ("B1", "B2", "B3", "B4", "B5", "B6", "oracle")
_PHASES = (
    ("baseline_validation", 12 * 60 * 60),
    ("singleton_b2", 24 * 60 * 60),
    ("promotion_b3", 24 * 60 * 60),
    ("final_validation", 12 * 60 * 60),
)
_TOTAL_BUDGET_SECONDS = 72 * 60 * 60

_REFERENCE_BASELINES = {
    "gcc_13_3_o2": {
        "profile_id": "gcc-13.3-o2",
        "frontend": "gcc",
        "tool": "riscv-gcc",
        "version": "13.3.0",
        "optimization": "-O2",
    },
    "clang_18_o3": {
        "profile_id": "clang-18-o3",
        "frontend": "clang",
        "tool": "clang",
        "version": "18.1.3",
        "optimization": "-O3",
    },
}
_REFERENCE_COMMON_SEMANTICS = [
    "-fwrapv",
    "-fno-fast-math",
    "-ffp-contract=off",
    "-ffreestanding",
    "-fno-builtin",
]
_HOTBLOCK_METRIC_SPECS = [
    {
        "metric_id": item["metric_id"],
        "source": item["source"],
        "pattern_sha256": sha256_json(item["pattern"]),
        "unit": item["unit"],
    }
    for item in cache_hotblock_metrics_v1()
]
_HOTBLOCK_RUNNER_ENVIRONMENT_KEY = "QEMU_HOTBLOCK_PLUGIN"


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read {label} as UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must contain a JSON object")
    return value


def _reference_toolchain_contract(
    path: Path,
    *,
    workspace_root: Path,
    measurement_protocols: Mapping[str, Mapping[str, Any]],
    measurement_protocol_paths: Mapping[str, Path],
) -> dict[str, Any]:
    snapshot = _read_json_object(path, label="reference toolchain snapshot")
    try:
        target = snapshot["target"]
        frontends = snapshot["reference_frontends"]
        proxy = snapshot["proxy_execution"]
        compile_driver = frontends["compile_driver_path"]
        common_semantics = frontends["common_semantics"]
    except KeyError as exc:
        raise ValidationError(f"reference toolchain snapshot lacks {exc.args[0]}") from exc
    if snapshot.get("schema") != "accela-toolchain-snapshot.v1":
        raise ValidationError("reference toolchain snapshot has an unsupported schema")
    if target != {"isa": "rv64gc", "abi": "lp64d", "code_model": "medany"}:
        raise ValidationError("reference toolchain snapshot target is not RV64GC/LP64D/medany")
    if common_semantics != _REFERENCE_COMMON_SEMANTICS:
        raise ValidationError("reference toolchain snapshot has drifted integer/FP semantics")
    validate_relative_path(compile_driver, label="reference compile driver path")
    compile_driver_sha256 = frontends.get("compile_driver_sha256")
    if not isinstance(compile_driver_sha256, str) or len(compile_driver_sha256) != 64:
        raise ValidationError("reference toolchain snapshot lacks a compile-driver SHA-256")
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise ConfigurationError("campaign workspace root must be a directory")
    snapshot_protocols = proxy.get("measurement_protocols")
    expected_modes = {"standard_proxy", "cache_hotblock"}
    if (
        not isinstance(snapshot_protocols, dict)
        or set(snapshot_protocols) != expected_modes
        or set(measurement_protocols) != expected_modes
        or set(measurement_protocol_paths) != expected_modes
    ):
        raise ValidationError(
            "reference toolchain snapshot must bind exactly both measurement protocols"
        )
    protocol_fields = {
        "measurement_mode", "protocol_id", "protocol_sha256", "protocol_path"
    }
    for mode in sorted(expected_modes):
        observed = snapshot_protocols[mode]
        expected = measurement_protocols[mode]
        if not isinstance(observed, dict) or set(observed) != protocol_fields:
            raise ValidationError(
                f"reference toolchain protocol binding has invalid fields: {mode}"
            )
        protocol_path = observed.get("protocol_path")
        if not isinstance(protocol_path, str):
            raise ValidationError(
                f"reference toolchain protocol path is invalid: {mode}"
            )
        validate_relative_path(
            protocol_path, label=f"reference toolchain {mode} protocol path"
        )
        recorded_path = (root / protocol_path).resolve(strict=True)
        try:
            recorded_path.relative_to(root)
        except ValueError as exc:
            raise ValidationError(
                f"reference toolchain protocol escapes the campaign workspace: {mode}"
            ) from exc
        supplied_path = measurement_protocol_paths[mode].resolve(strict=True)
        if recorded_path != supplied_path or (
            observed["measurement_mode"] != mode
            or observed["protocol_id"] != expected["protocol_id"]
            or observed["protocol_sha256"] != expected["protocol_sha256"]
        ):
            raise ValidationError(
                f"reference toolchain protocol binding differs from supplied protocol: {mode}"
            )
    driver_file = (root / compile_driver).resolve(strict=True)
    try:
        driver_file.relative_to(root)
    except ValueError as exc:
        raise ValidationError("reference compile driver escapes the campaign workspace") from exc
    if not driver_file.is_file() or sha256_file(driver_file) != compile_driver_sha256:
        raise ValidationError("reference compile driver physical hash differs from snapshot")
    baselines: list[dict[str, Any]] = []
    for baseline_id, expected in _REFERENCE_BASELINES.items():
        observed = frontends.get(expected["frontend"])
        if not isinstance(observed, dict):
            raise ValidationError(f"reference toolchain snapshot lacks {expected['frontend']}")
        if (
            observed.get("version") != expected["version"]
            or observed.get("optimization") != expected["optimization"]
        ):
            raise ValidationError(f"reference toolchain baseline drift: {baseline_id}")
        command = [
            "sh",
            compile_driver,
            expected["frontend"],
            "{source}",
            "{artifact}",
        ]
        profile_sha256 = sha256_json(
            {
                "compiler_baseline": baseline_id,
                "flags": [expected["optimization"]],
            }
        )
        baselines.append(
            {
                "compiler_baseline": baseline_id,
                "profile_id": expected["profile_id"],
                "profile_sha256": profile_sha256,
                "tool": expected["tool"],
                "version": expected["version"],
                "optimization": expected["optimization"],
                "compiler_executable": "sh",
                "compiler_command_sha256": sha256_json(
                    {"command": command, "environment": {}}
                ),
            }
        )
    common_tool_versions = {
        "qemu-system-riscv64": proxy.get("qemu_system_riscv64"),
        "bare-metal-linker": proxy.get("riscv_bare_metal_linker"),
        "python": proxy.get("python"),
        "glib": proxy.get("glib"),
    }
    accela_jdk = proxy.get("jdk")
    if any(not isinstance(value, str) or not value for value in common_tool_versions.values()):
        raise ValidationError("reference toolchain snapshot lacks common measured tool versions")
    if not isinstance(accela_jdk, str) or not accela_jdk:
        raise ValidationError("reference toolchain snapshot lacks the ACCELA JDK version")
    return {
        "snapshot_sha256": sha256_file(path.resolve(strict=True)),
        "compile_driver_sha256": compile_driver_sha256,
        "common_tool_versions": common_tool_versions,
        "accela_jdk_version": accela_jdk,
        "baselines": baselines,
    }


def _measurement_protocol_contract(
    path: Path, *, expected_mode: str
) -> dict[str, Any]:
    protocol = load_and_validate(path)
    if protocol["schema_version"] != "measurement-protocol.v1":
        raise ValidationError("campaign requires measurement-protocol.v1")
    if protocol["measurement_mode"] != expected_mode:
        raise ValidationError(
            f"campaign {expected_mode} protocol has the wrong measurement mode"
        )
    return {
        "protocol_id": protocol["protocol_id"],
        "measurement_mode": protocol["measurement_mode"],
        "protocol_sha256": sha256_json(protocol),
        "runner_command_sha256": protocol["qemu"]["runner_command_sha256"],
        "runner_adapter": protocol["qemu"]["runner_adapter"],
        "profile_plugin_sha256": protocol["plugin_binaries"]["profile_sha256"],
        "cache_plugin_sha256": protocol["plugin_binaries"]["cache_sha256"],
        "hotblocks_plugin_sha256": protocol["plugin_binaries"]["hotblocks_sha256"],
        "cache_model_sha256": sha256_json(protocol["cache_model"]),
    }


def _parse_timestamp(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ConfigurationError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _task_id(phase: str, role: str, profile_id: str, measurement_mode: str) -> str:
    return f"task:{phase}:{role}:{safe_slug(profile_id)}:{measurement_mode}"


def build_campaign_plan(
    *,
    matrix_path: Path,
    suite_paths: Mapping[str, Path],
    oracle_plan_path: Path,
    measurement_protocol_path: Path,
    hotblock_measurement_protocol_path: Path,
    reference_toolchain_path: Path,
    workspace_root: Path,
    campaign_id: str,
    max_workers: int,
) -> dict[str, Any]:
    """Build the singleton-only initial 72-hour campaign schedule.

    Pair profiles are deliberately absent until B3 confirmation selects a
    measured Top5.  ``finalize_campaign_plan`` then creates a new, hash-bound
    plan containing exactly the ten selected pairs.
    """

    matrix = load_and_validate(matrix_path)
    if matrix["schema_version"] != "ablation-matrix.v1":
        raise ValidationError("campaign plan requires ablation-matrix.v1")
    if not 1 <= max_workers <= 4:
        raise ConfigurationError("campaign max_workers must be between 1 and 4")
    measurement_protocols = {
        "standard_proxy": _measurement_protocol_contract(
            measurement_protocol_path, expected_mode="standard_proxy"
        ),
        "cache_hotblock": _measurement_protocol_contract(
            hotblock_measurement_protocol_path, expected_mode="cache_hotblock"
        ),
    }
    if (
        measurement_protocols["standard_proxy"]["protocol_sha256"]
        == measurement_protocols["cache_hotblock"]["protocol_sha256"]
        or measurement_protocols["standard_proxy"]["runner_command_sha256"]
        == measurement_protocols["cache_hotblock"]["runner_command_sha256"]
    ):
        raise ValidationError("standard and cache-hotblock protocols must be distinct snapshots")
    reference_toolchain = _reference_toolchain_contract(
        reference_toolchain_path,
        workspace_root=workspace_root,
        measurement_protocols=measurement_protocols,
        measurement_protocol_paths={
            "standard_proxy": measurement_protocol_path,
            "cache_hotblock": hotblock_measurement_protocol_path,
        },
    )
    if set(suite_paths) != set(_SUITE_ROLES):
        missing = sorted(set(_SUITE_ROLES) - set(suite_paths))
        extra = sorted(set(suite_paths) - set(_SUITE_ROLES))
        raise ConfigurationError(
            "campaign requires exactly B1..B6 and oracle manifests"
            f" (missing={','.join(missing) or '-'}; extra={','.join(extra) or '-'})"
        )
    manifests: dict[str, dict[str, Any]] = {}
    suites: list[dict[str, Any]] = []
    for role in _SUITE_ROLES:
        manifest = load_and_validate(suite_paths[role])
        if manifest["schema_version"] != "benchmark-manifest.v1":
            raise ValidationError(f"campaign suite {role} is not benchmark-manifest.v1")
        if (
            {case["target"] for case in manifest["cases"]} != {"rv64gc"}
            or manifest["provenance"]["data_role"] != role
        ):
            raise ValidationError(f"campaign suite {role} has inconsistent target/data_role")
        manifests[role] = manifest
        suites.append(
            {
                "data_role": role,
                "suite_id": manifest["suite_id"],
                "manifest_sha256": sha256_json(manifest),
                "case_count": len(manifest["cases"]),
            }
        )
    parent = manifests["B2"]["provenance"]["derived_from"]
    if parent is None or parent["manifest_sha256"] != sha256_json(manifests["B3"]):
        raise ValidationError("campaign B2 must be the hash-bound subset of the supplied B3 manifest")
    oracle_plan = load_and_validate(oracle_plan_path)
    if (
        oracle_plan["schema_version"] != "oracle-plan.v1"
        or oracle_plan["evidence_class"] != "cleanroom"
        or oracle_plan["manifest_data_role"] != "oracle"
        or oracle_plan["suite_id"] != manifests["oracle"]["suite_id"]
        or oracle_plan["manifest_sha256"] != sha256_json(manifests["oracle"])
    ):
        raise ValidationError("campaign Oracle plan must be bound to the supplied clean-room manifest")

    profiles = {item["profile_id"]: item for item in matrix["profiles"]}
    full = profiles.get("full")
    mandatory = profiles.get("mandatory")
    if full is None or full["kind"] != "full" or mandatory is None or mandatory["kind"] != "mandatory":
        raise ValidationError("campaign matrix requires exactly named full and mandatory profiles")
    if oracle_plan["pipeline_profile"] != {
        "profile_id": full["profile_id"],
        "profile_sha256": full["profile_sha256"],
    }:
        raise ValidationError("campaign Oracle plan must use the exact matrix FULL pipeline")
    family_profiles = [item for item in matrix["profiles"] if item["kind"] == "family_ablation"]
    if len(family_profiles) < 8:
        raise ValidationError("campaign needs at least eight schedulable singleton families")
    pair_profiles = [item for item in matrix["profiles"] if item["kind"] == "pair_ablation"]
    if pair_profiles:
        raise ValidationError(
            "initial campaign matrix must contain only FULL, mandatory, and singleton profiles"
        )
    for profile in matrix["profiles"]:
        validate_relative_path(profile["path"], label="campaign profile path")

    suite_by_role = {item["data_role"]: item for item in suites}
    reference_by_baseline = {
        item["compiler_baseline"]: item for item in reference_toolchain["baselines"]
    }
    tasks: list[dict[str, Any]] = []
    task_by_key: dict[tuple[str, str, str], str] = {}

    def add_task(
        *,
        phase_id: str,
        role: str,
        profile: Mapping[str, Any],
        selection_rule: str,
        measurement_mode: str | None = None,
        dependency_ids: tuple[str, ...] = (),
        compiler_baseline: str = "accela_full",
        oracle_leg: str | None = None,
    ) -> str:
        resolved_measurement_mode = measurement_mode or (
            "correctness" if role == "B1" else "standard_proxy"
        )
        task_id = _task_id(
            phase_id, role, profile["profile_id"], resolved_measurement_mode
        )
        if oracle_leg is not None:
            task_id = f"{task_id}:{oracle_leg}"
        if any(item["task_id"] == task_id for item in tasks):
            raise ValidationError(f"campaign generated duplicate task id: {task_id}")
        suite = suite_by_role[role]
        run_id = f"campaign:{safe_slug(campaign_id)}:{safe_slug(task_id)}"
        if resolved_measurement_mode == "correctness":
            measurement_contract = {
                "metric_profile_id": None,
                "compile_repetitions": 5,
                "reuse_compile_cache": False,
                "additional_metric_specs": [],
            }
        else:
            measurement_contract = {
                "metric_profile_id": "rv64gc-qemu-v1",
                "compile_repetitions": 5,
                "reuse_compile_cache": False,
                "additional_metric_specs": (
                    deepcopy(_HOTBLOCK_METRIC_SPECS)
                    if resolved_measurement_mode == "cache_hotblock"
                    else []
                ),
            }
        reference_contract = reference_by_baseline.get(compiler_baseline)
        tasks.append(
            {
                "task_id": task_id,
                "suite_role": role,
                "suite_id": suite["suite_id"],
                "manifest_sha256": suite["manifest_sha256"],
                "profile_id": profile["profile_id"],
                "profile_sha256": profile["profile_sha256"],
                "profile_path": profile["path"],
                "kind": profile["kind"],
                "logical_families": list(profile["logical_families"]),
                "phase_id": phase_id,
                "selection_rule": selection_rule,
                "measurement_mode": resolved_measurement_mode,
                "measurement_contract": measurement_contract,
                "required_evidence_level": (
                    "qemu_correctness" if role == "B1" else "qemu_proxy"
                ),
                "run_id": run_id,
                "dependencies": list(dependency_ids),
                "compiler_baseline": compiler_baseline,
                "reference_compiler_contract": (
                    deepcopy(reference_contract) if reference_contract is not None else None
                ),
                "oracle_leg": oracle_leg,
            }
        )
        task_by_key[(phase_id, role, profile["profile_id"])] = task_id
        return task_id

    # First 12h: rules/toolchain self-check plus B1 correctness and the four
    # official B3 baselines.  Holdout/mature/oracle work is deliberately late.
    add_task(phase_id="baseline_validation", role="B1", profile=full, selection_rule="always")
    add_task(phase_id="baseline_validation", role="B3", profile=full, selection_rule="always")
    add_task(
        phase_id="baseline_validation",
        role="B3",
        profile=mandatory,
        selection_rule="always",
        dependency_ids=(task_by_key[("baseline_validation", "B3", "full")],),
        compiler_baseline="accela_mandatory",
    )
    for compiler_baseline in ("gcc_13_3_o2", "clang_18_o3"):
        reference = reference_by_baseline[compiler_baseline]
        descriptor = {
            "profile_id": reference["profile_id"],
            "profile_sha256": reference["profile_sha256"],
            "path": None,
            "kind": "toolchain_baseline",
            "logical_families": [],
        }
        add_task(
            phase_id="baseline_validation",
            role="B3",
            profile=descriptor,
            selection_rule="always",
            compiler_baseline=compiler_baseline,
        )

    b2_full = add_task(
        phase_id="singleton_b2", role="B2", profile=full, selection_rule="always"
    )

    for profile in family_profiles:
        smoke = add_task(
            phase_id="singleton_b2",
            role="B2",
            profile=profile,
            selection_rule="always",
            dependency_ids=(b2_full,),
        )
        add_task(
            phase_id="promotion_b3",
            role="B3",
            profile=profile,
            selection_rule="smoke_promoted",
            dependency_ids=(
                smoke,
                task_by_key[("baseline_validation", "B3", "full")],
            ),
        )

    for profile in family_profiles:
        promotion_task = task_by_key[("promotion_b3", "B3", profile["profile_id"])]
        for role in ("B4", "B5", "B6"):
            full_key = ("final_validation", role, "full")
            if full_key not in task_by_key:
                add_task(
                    phase_id="final_validation",
                    role=role,
                    profile=full,
                    selection_rule="confirmation_available",
                )
            add_task(
                phase_id="final_validation",
                role=role,
                profile=profile,
                selection_rule="confirmation_top5",
                dependency_ids=(
                    promotion_task,
                    task_by_key[full_key],
                ),
            )
        add_task(
            phase_id="final_validation",
            role="B3",
            profile=profile,
            selection_rule="confirmation_top5",
            measurement_mode="cache_hotblock",
            dependency_ids=(
                promotion_task,
                task_by_key[("baseline_validation", "B3", "full")],
            ),
        )

    for leg in ("baseline", "optimized"):
        oracle_profile = {
            "profile_id": full["profile_id"],
            "profile_sha256": full["profile_sha256"],
            "path": full["path"],
            "kind": full["kind"],
            "logical_families": [],
        }
        task_id = add_task(
            phase_id="final_validation",
            role="oracle",
            profile=oracle_profile,
            selection_rule="confirmation_available",
            compiler_baseline="oracle_accela_full",
            oracle_leg=leg,
        )
        tasks[-1]["run_id"] = oracle_plan[f"{leg}_run_id"]

    return validate_document(
        {
            "schema_version": "campaign-plan.v1",
            "campaign_id": campaign_id,
            "initial_matrix_sha256": sha256_json(matrix),
            "matrix_sha256": sha256_json(matrix),
            "parent_plan_sha256": None,
            "promotion_status_sha256": None,
            "total_budget_seconds": _TOTAL_BUDGET_SECONDS,
            "max_workers": max_workers,
            "measurement_protocols": measurement_protocols,
            "reference_toolchain": reference_toolchain,
            "oracle_plan": {
                "plan_sha256": sha256_json(oracle_plan),
                "suite_id": oracle_plan["suite_id"],
                "manifest_sha256": oracle_plan["manifest_sha256"],
                "baseline_run_id": oracle_plan["baseline_run_id"],
                "optimized_run_id": oracle_plan["optimized_run_id"],
                "pipeline_profile_id": oracle_plan["pipeline_profile"]["profile_id"],
                "pipeline_profile_sha256": oracle_plan["pipeline_profile"]["profile_sha256"],
            },
            "suites": suites,
            "promotion": {
                "smoke_geometric_mean_minimum": 1.005,
                "smoke_any_case_minimum": 1.10,
                "smoke_regression_ratio_below": 0.97,
                "promote_correctness_anomaly": True,
                "minimum_promoted_profiles": 8,
                "final_profile_count": 5,
                "required_pair_profiles": 10,
                "required_evidence_level": "qemu_proxy",
            },
            "phases": [
                {
                    "phase_id": phase_id,
                    "budget_seconds": budget,
                    "unused_budget_destination": (
                        None if phase_id == "final_validation" else "final_validation"
                    ),
                }
                for phase_id, budget in _PHASES
            ],
            "final_pair_families": [],
            "tasks": tasks,
        }
    )


def finalize_campaign_plan(
    *,
    plan_path: Path,
    status_path: Path,
    matrix_path: Path,
) -> dict[str, Any]:
    """Create the immutable Top5-pair plan from measured promotion evidence."""

    initial = load_and_validate(plan_path)
    status = load_and_validate(status_path)
    matrix = load_and_validate(matrix_path)
    if initial["schema_version"] != "campaign-plan.v1":
        raise ValidationError("campaign finalization requires campaign-plan.v1")
    if initial["parent_plan_sha256"] is not None or initial["final_pair_families"]:
        raise ValidationError("only an unfinalized campaign plan can be finalized")
    initial_digest = sha256_json(initial)
    if (
        status["schema_version"] != "campaign-status.v1"
        or status["plan_sha256"] != initial_digest
        or status["campaign_id"] != initial["campaign_id"]
    ):
        raise ValidationError("promotion status is not bound to the initial campaign plan")
    if matrix["schema_version"] != "ablation-matrix.v1":
        raise ValidationError("campaign finalization requires ablation-matrix.v1")

    final_profiles = status["promotion_decisions"]["final_profile_ids"]
    if len(final_profiles) != initial["promotion"]["final_profile_count"]:
        raise ValidationError("campaign finalization requires five eligible B3 confirmation profiles")
    if any(not profile_id.startswith("without.") for profile_id in final_profiles):
        raise ValidationError("campaign Top5 entries must be singleton family-ablation profiles")
    selected_families = sorted(profile_id.removeprefix("without.") for profile_id in final_profiles)
    if len(set(selected_families)) != 5:
        raise ValidationError("campaign Top5 contains duplicate logical families")

    initial_profiles: dict[str, dict[str, Any]] = {}
    for task in initial["tasks"]:
        if task["kind"] == "toolchain_baseline":
            continue
        identity = {
            "profile_id": task["profile_id"],
            "profile_sha256": task["profile_sha256"],
            "path": task["profile_path"],
            "kind": task["kind"],
            "logical_families": task["logical_families"],
        }
        previous = initial_profiles.setdefault(task["profile_id"], identity)
        if previous != identity:
            raise ValidationError("initial campaign reuses a profile id with different content")
    matrix_profiles = {item["profile_id"]: item for item in matrix["profiles"]}
    if matrix["registry_sha256"] is None:
        raise AssertionError("validated matrix always contains registry_sha256")
    for profile_id, identity in initial_profiles.items():
        item = matrix_profiles.get(profile_id)
        if item != identity:
            raise ValidationError(f"extended matrix changed initial profile identity: {profile_id}")

    pair_profiles = [item for item in matrix["profiles"] if item["kind"] == "pair_ablation"]
    expected_pairs = {
        tuple(sorted((selected_families[left], selected_families[right])))
        for left in range(5)
        for right in range(left + 1, 5)
    }
    actual_pairs = {tuple(sorted(item["logical_families"])) for item in pair_profiles}
    if len(pair_profiles) != 10 or actual_pairs != expected_pairs:
        raise ValidationError("extended campaign matrix must contain exactly all ten measured Top5 pairs")
    if any(item["profile_id"] in initial_profiles for item in pair_profiles):
        raise ValidationError("pair profile id collides with an initial campaign profile")

    tasks = deepcopy(initial["tasks"])
    suite = next(item for item in initial["suites"] if item["data_role"] == "B3")
    task_by_key = {
        (task["phase_id"], task["suite_role"], task["profile_id"]): task["task_id"]
        for task in tasks
    }
    for profile in sorted(pair_profiles, key=lambda item: item["profile_id"]):
        validate_relative_path(profile["path"], label="campaign profile path")
        dependencies = [
            task_by_key[("promotion_b3", "B3", f"without.{family}")]
            for family in profile["logical_families"]
        ]
        dependencies.append(task_by_key[("baseline_validation", "B3", "full")])
        task_id = _task_id("final_validation", "B3", profile["profile_id"], "standard_proxy")
        tasks.append(
            {
                "task_id": task_id,
                "suite_role": "B3",
                "suite_id": suite["suite_id"],
                "manifest_sha256": suite["manifest_sha256"],
                "profile_id": profile["profile_id"],
                "profile_sha256": profile["profile_sha256"],
                "profile_path": profile["path"],
                "kind": profile["kind"],
                "logical_families": list(profile["logical_families"]),
                "phase_id": "final_validation",
                "selection_rule": "pair_of_confirmation_top5",
                "measurement_mode": "standard_proxy",
                "measurement_contract": {
                    "metric_profile_id": "rv64gc-qemu-v1",
                    "compile_repetitions": 5,
                    "reuse_compile_cache": False,
                    "additional_metric_specs": [],
                },
                "required_evidence_level": "qemu_proxy",
                "run_id": f"campaign:{safe_slug(initial['campaign_id'])}:{safe_slug(task_id)}",
                "dependencies": dependencies,
                "compiler_baseline": "accela_full",
                "reference_compiler_contract": None,
                "oracle_leg": None,
            }
        )

    finalized = deepcopy(initial)
    finalized.update(
        initial_matrix_sha256=initial["initial_matrix_sha256"],
        matrix_sha256=sha256_json(matrix),
        parent_plan_sha256=initial_digest,
        promotion_status_sha256=sha256_json(status),
        final_pair_families=selected_families,
        tasks=tasks,
    )
    return validate_document(finalized)


def _require_campaign_correctness(run: Mapping[str, Any]) -> None:
    configuration = run["configuration"]
    if (
        configuration["evidence_level"] != "qemu_correctness"
        or configuration["runner"]["kind"] != "qemu"
        or configuration["metric_profile_id"] is not None
        or configuration["primary_metric_id"] != "wall_time_ns"
        or configuration["output_contract"] == "raw_stdout"
        or configuration["compile_repetitions"] != 5
        or configuration["reuse_compile_cache"]
    ):
        raise ValidationError("campaign B1 run is not strict QEMU correctness evidence")
    if (
        configuration["compiler"]["kind"] != "benchmark-compiler"
        or configuration.get("pipeline_profile_file_sha256")
        != run["provenance"]["pipeline_profile_sha256"]
        or configuration["remarks_file_sha256"] is None
    ):
        raise ValidationError("campaign B1 run lacks the physical ACCELA pipeline/remarks binding")
    if (
        run["provenance"]["measurement_protocol_id"] is not None
        or run["provenance"]["measurement_protocol_sha256"] is not None
    ):
        raise ValidationError("campaign B1 correctness must not claim the performance protocol")
    if not configuration["tool_versions"]:
        raise ValidationError("campaign B1 correctness lacks toolchain version evidence")
    for case in run["cases"]:
        if case["status"] != "passed":
            continue
        if (
            case["cache_hit"]
            or case["compile"] is None
            or case["compile"]["status"] != "ok"
            or len(case["compile_samples"]) != 5
            or case["compile_statistics"] is None
            or case["link"] is None
            or case["link"]["status"] != "ok"
        ):
            raise ValidationError("campaign B1 passed case lacks strict compile/link evidence")


def _require_hotblock_evidence(run: Mapping[str, Any]) -> None:
    configuration = run["configuration"]
    preset = rv64gc_qemu_v1()
    base_ids = {preset["primary_metric_id"]} | {
        item["metric_id"] for item in preset["additional"]
    }
    configured = {item["metric_id"]: item for item in configuration["metrics"]}
    expected_extra = {item["metric_id"]: item for item in _HOTBLOCK_METRIC_SPECS}
    if set(configured) != base_ids | set(expected_extra):
        raise ValidationError(
            "cache_hotblock campaign run requires exactly the standard catalog plus normalized hotblock metrics"
        )
    for metric_id, expected in expected_extra.items():
        if configured[metric_id] != expected:
            raise ValidationError(f"cache_hotblock metric specification drift: {metric_id}")
    if _HOTBLOCK_RUNNER_ENVIRONMENT_KEY not in configuration["runner"]["environment_keys"]:
        raise ValidationError(
            "cache_hotblock runner does not bind the verified hotblock plugin asset"
        )
    for case in run["cases"]:
        if case["status"] != "passed":
            continue
        for sample in case["samples"]:
            if sample["status"] != "passed":
                continue
            sample_observed = {
                item["metric_id"]: item for item in sample["measurements"]
            }
            for metric_id in expected_extra:
                measurement = sample_observed.get(metric_id)
                if (
                    measurement is None
                    or measurement["availability"] != "measured"
                    or measurement["origin"] != "observed"
                    or measurement["value"] is None
                ):
                    raise ValidationError(
                        "cache_hotblock passed sample lacks observed normalized evidence: "
                        + metric_id
                    )


def _validate_campaign_run(
    plan: Mapping[str, Any], task: Mapping[str, Any], run: Mapping[str, Any]
) -> None:
    oracle_leg = task["oracle_leg"]
    identity_mismatch = (
        run["run_id"] != task["run_id"]
        or run["provenance"]["pipeline_profile_id"] != task["profile_id"]
        or run["provenance"]["pipeline_profile_sha256"] != task["profile_sha256"]
        or run["configuration"]["evidence_level"] != task["required_evidence_level"]
    )
    if oracle_leg is None:
        identity_mismatch = identity_mismatch or (
            run["suite_id"] != task["suite_id"]
            or run["manifest_sha256"] != task["manifest_sha256"]
        )
    else:
        identity_mismatch = identity_mismatch or (
            run["suite_id"] != f"{plan['oracle_plan']['suite_id']}-{oracle_leg}"
            or any(
                case["data_role"] != "oracle"
                or case["oracle_pair"] is None
                or case["oracle_pair"]["leg"] != oracle_leg
                for case in run["cases"]
            )
        )

    contract = task["measurement_contract"]
    configuration = run["configuration"]
    identity_mismatch = identity_mismatch or (
        configuration["metric_profile_id"] != contract["metric_profile_id"]
        or configuration["compile_repetitions"] != contract["compile_repetitions"]
        or configuration["reuse_compile_cache"] != contract["reuse_compile_cache"]
        or contract["additional_metric_specs"]
        != (
            _HOTBLOCK_METRIC_SPECS
            if task["measurement_mode"] == "cache_hotblock"
            else []
        )
    )
    measurement_mode = task["measurement_mode"]
    compiler_baseline = task["compiler_baseline"]
    is_accela = compiler_baseline in {
        "accela_full",
        "accela_mandatory",
        "oracle_accela_full",
    }
    versions = {item["tool"]: item for item in configuration["tool_versions"]}
    for tool, expected_version in plan["reference_toolchain"]["common_tool_versions"].items():
        observed_common = versions.get(tool)
        if observed_common is None or observed_common["actual"] != expected_version:
            identity_mismatch = True
    if is_accela:
        observed_jdk = versions.get("accela-jdk")
        if (
            observed_jdk is None
            or observed_jdk["actual"] != plan["reference_toolchain"]["accela_jdk_version"]
        ):
            identity_mismatch = True
    if measurement_mode == "correctness":
        _require_campaign_correctness(run)
    else:
        protocol = plan["measurement_protocols"][measurement_mode]
        identity_mismatch = identity_mismatch or (
            run["provenance"]["measurement_protocol_id"] != protocol["protocol_id"]
            or run["provenance"]["measurement_protocol_sha256"]
            != protocol["protocol_sha256"]
            or configuration["runner"]["command_sha256"]
            != protocol["runner_command_sha256"]
            or configuration["runner"]["adapter"] != protocol["runner_adapter"]
        )
        _require_formal_measurement(
            run,
            require_accela_pipeline=is_accela,
            allow_metric_superset=measurement_mode == "cache_hotblock",
        )
        if measurement_mode == "cache_hotblock":
            _require_hotblock_evidence(run)

    if is_accela:
        identity_mismatch = identity_mismatch or (
            configuration["compiler"]["kind"] != "benchmark-compiler"
            or task["reference_compiler_contract"] is not None
        )
    else:
        reference = task["reference_compiler_contract"]
        if reference is None:
            raise ValidationError("reference compiler task lacks a frozen compiler contract")
        observed = versions.get(reference["tool"])
        identity_mismatch = identity_mismatch or (
            configuration["compiler"]["kind"] != "external"
            or configuration["compiler"]["executable"] != reference["compiler_executable"]
            or configuration["compiler"]["command_sha256"]
            != reference["compiler_command_sha256"]
            or run["provenance"]["compiler_artifact_sha256"]
            != plan["reference_toolchain"]["snapshot_sha256"]
            or observed is None
            or observed["actual"] != reference["version"]
            or observed["official_expected"] != reference["version"]
            or observed["comparison"] != "exact"
        )
    if identity_mismatch:
        raise ValidationError(f"campaign run provenance differs from task: {task['task_id']}")


def _load_campaign_runs(
    plan: Mapping[str, Any], run_paths: Mapping[str, Path]
) -> dict[str, dict[str, Any]]:
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    unknown = sorted(set(run_paths) - set(tasks))
    if unknown:
        raise ConfigurationError("campaign status references unknown task ids: " + ", ".join(unknown))
    result: dict[str, dict[str, Any]] = {}
    for task_id, path in run_paths.items():
        task = tasks[task_id]
        run = load_and_validate(path)
        if run["schema_version"] != "run-record.v1":
            raise ValidationError("campaign run evidence must be run-record.v1")
        _validate_campaign_run(plan, task, run)
        result[task_id] = run
    return result


def _study_decisions(
    *,
    plan: Mapping[str, Any],
    runs: Mapping[str, Mapping[str, Any]],
    study_paths: Mapping[str, Path],
) -> dict[str, Any]:
    allowed = {"singleton_b2", "promotion_b3"}
    if not set(study_paths).issubset(allowed):
        raise ConfigurationError("campaign studies must be singleton_b2 or promotion_b3")
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    by_phase_profile = {
        (task["phase_id"], task["profile_id"]): task for task in plan["tasks"]
    }

    loaded: dict[str, dict[str, Any]] = {}
    study_refs: list[dict[str, Any]] = []
    for phase_id, path in sorted(study_paths.items()):
        study = load_and_validate(path)
        if study["schema_version"] != "ablation-study.v1" or study["matrix_sha256"] != plan["initial_matrix_sha256"]:
            raise ValidationError(f"campaign {phase_id} study is not bound to the campaign matrix")
        role = "B2" if phase_id == "singleton_b2" else "B3"
        baseline_task = next(
            task
            for task in plan["tasks"]
            if task["suite_role"] == role
            and task["profile_id"] == "full"
            and task["oracle_leg"] is None
        )
        baseline_run = runs.get(baseline_task["task_id"])
        if baseline_run is None or study["baseline_run_id"] != baseline_run["run_id"]:
            raise ValidationError(f"campaign {phase_id} study lacks its exact FULL run evidence")
        for variant in study["variants"]:
            task = by_phase_profile.get((phase_id, variant["profile_id"]))
            run = None if task is None else runs.get(task["task_id"])
            if task is None or run is None or variant["run_id"] != run["run_id"]:
                raise ValidationError(f"campaign {phase_id} study variant is not bound to a scheduled run")
        loaded[phase_id] = study
        study_refs.append(
            {
                "phase_id": phase_id,
                "study_id": study["study_id"],
                "study_sha256": sha256_json(study),
                "baseline_run_id": study["baseline_run_id"],
            }
        )

    promotion = plan["promotion"]
    smoke_rows: list[dict[str, Any]] = []
    promoted: set[str] = set()
    smoke = loaded.get("singleton_b2")
    if smoke is not None:
        for variant in smoke["variants"]:
            ratios = [float(item["contribution_ratio"]) for item in variant["per_cases"]]
            reasons: list[str] = []
            gm = variant["case_geometric_mean_contribution"]
            if gm is not None and gm >= promotion["smoke_geometric_mean_minimum"]:
                reasons.append("geometric_mean_threshold")
            if ratios and max(ratios) >= promotion["smoke_any_case_minimum"]:
                reasons.append("single_case_threshold")
            if ratios and min(ratios) < promotion["smoke_regression_ratio_below"]:
                reasons.append("regression_investigation")
            if variant["correctness_failures"] > 0:
                reasons.append("correctness_investigation")
            if reasons:
                promoted.add(variant["profile_id"])
            smoke_rows.append(
                {
                    "profile_id": variant["profile_id"],
                    "geometric_mean_contribution": gm,
                    "maximum_case_contribution": max(ratios) if ratios else None,
                    "minimum_case_contribution": min(ratios) if ratios else None,
                    "correctness_failures": variant["correctness_failures"],
                    "selected": False,
                    "reasons": reasons,
                }
            )
        # The minimum-Top8 rule fills only from variants with comparable smoke
        # measurements.  A tool failure/no-comparable-pair row has no benefit
        # evidence and must not become "promoted" merely because the schedule
        # has fewer than eight measured candidates.  Correctness anomalies are
        # already selected explicitly above, even when they have no pair.
        ranked = sorted(
            (
                row
                for row in smoke_rows
                if row["geometric_mean_contribution"] is not None
            ),
            key=lambda row: (
                -(row["geometric_mean_contribution"] or -math.inf),
                row["profile_id"],
            ),
        )
        for row in ranked:
            if len(promoted) >= promotion["minimum_promoted_profiles"]:
                break
            if row["profile_id"] not in promoted:
                promoted.add(row["profile_id"])
                row["reasons"].append("minimum_top8_fill")
        for row in smoke_rows:
            row["selected"] = row["profile_id"] in promoted

    confirmation = loaded.get("promotion_b3")
    confirmation_rows: list[dict[str, Any]] = []
    final_profiles: list[str] = []
    if confirmation is not None:
        for variant in confirmation["variants"]:
            if variant["profile_id"] not in promoted:
                raise ValidationError("B3 confirmation study contains a profile not promoted by B2")
            confirmation_rows.append(
                {
                    "profile_id": variant["profile_id"],
                    "geometric_mean_contribution": variant["case_geometric_mean_contribution"],
                    "eligible_for_ranking": variant["eligible_for_ranking"],
                    "ineligibility_reason": variant["ineligibility_reason"],
                }
            )
        eligible = sorted(
            (row for row in confirmation_rows if row["eligible_for_ranking"]),
            key=lambda row: (
                -(row["geometric_mean_contribution"] or -math.inf),
                row["profile_id"],
            ),
        )
        final_profiles = [row["profile_id"] for row in eligible[: promotion["final_profile_count"]]]

    final_families = sorted(profile.removeprefix("without.") for profile in final_profiles)
    pair_coverage_complete = (
        len(final_profiles) == promotion["final_profile_count"]
        and final_families == plan["final_pair_families"]
    )
    return {
        "study_refs": study_refs,
        "smoke": smoke_rows,
        "promoted_profile_ids": sorted(promoted),
        "minimum_top8_satisfied": len(promoted) >= promotion["minimum_promoted_profiles"],
        "confirmation": confirmation_rows,
        "final_profile_ids": final_profiles,
        "final_pair_coverage_complete": pair_coverage_complete,
    }


def _task_selection(task: Mapping[str, Any], decisions: Mapping[str, Any]) -> tuple[str, list[str]]:
    rule = task["selection_rule"]
    if rule == "always":
        return "selected", ["unconditional"]
    if rule == "smoke_promoted":
        if not decisions["smoke"]:
            return "awaiting_evidence", []
        if task["profile_id"] in decisions["promoted_profile_ids"]:
            row = next(row for row in decisions["smoke"] if row["profile_id"] == task["profile_id"])
            return "selected", list(row["reasons"])
        return "not_selected", ["promotion_threshold_not_met"]
    if rule == "confirmation_available":
        return (
            ("selected", ["confirmation_top5_available"])
            if len(decisions["final_profile_ids"]) == 5
            else ("awaiting_evidence", [])
        )
    if not decisions["confirmation"]:
        return "awaiting_evidence", []
    if rule == "confirmation_top5":
        return (
            ("selected", ["confirmation_top5"])
            if task["profile_id"] in decisions["final_profile_ids"]
            else ("not_selected", ["top5_not_selected"])
        )
    if rule == "pair_of_confirmation_top5":
        selected_families = {
            profile.removeprefix("without.") for profile in decisions["final_profile_ids"]
        }
        return (
            ("selected", ["all_pair_members_in_confirmation_top5"])
            if set(task["logical_families"]).issubset(selected_families)
            else ("not_selected", ["top5_pair_not_selected"])
        )
    raise AssertionError(f"unknown campaign selection rule: {rule}")


def _dependency_outcome(
    task: Mapping[str, Any],
    task_status: Mapping[str, Any],
    dependency_task: Mapping[str, Any],
    dependency_status: Mapping[str, Any],
) -> str:
    """Classify dependency evidence without treating every terminal run as success.

    A failed B2 singleton is usable only as diagnostic evidence for its own B3
    confirmation task, and only after the B2 study has identified a correctness
    anomaly.  Timeout, tool failure, and failures of unrelated dependencies
    remain hard dependency failures.
    """

    if dependency_status["status"] == "completed":
        return "successful_completion"
    correctness_diagnostic = (
        task["selection_rule"] == "smoke_promoted"
        and task["phase_id"] == "promotion_b3"
        and task["suite_role"] == "B3"
        and dependency_task["phase_id"] == "singleton_b2"
        and dependency_task["suite_role"] == "B2"
        and dependency_task["profile_id"] == task["profile_id"]
        and "correctness_investigation" in task_status["selection_reasons"]
        and dependency_status["status"] == "failed"
        and dependency_status["missing_reason"] == "correctness_failure"
    )
    if correctness_diagnostic:
        return "terminal_diagnostic_evidence"
    if dependency_status["status"] in {
        "failed",
        "interrupted",
        "not_selected",
        "budget_exhausted",
    }:
        return "terminal_failure"
    return "waiting"


def _dependency_outcomes(
    task: Mapping[str, Any],
    *,
    tasks: Mapping[str, Mapping[str, Any]],
    statuses: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    return [
        _dependency_outcome(
            task,
            statuses[task["task_id"]],
            tasks[dependency],
            statuses[dependency],
        )
        for dependency in task["dependencies"]
    ]


def _dependencies_ready(outcomes: list[str]) -> bool:
    return all(
        outcome in {"successful_completion", "terminal_diagnostic_evidence"}
        for outcome in outcomes
    )


def _run_end(run: Mapping[str, Any]) -> datetime:
    return _parse_timestamp(run["completed_at"] or run["updated_at"], label="run end")


def _failed_run_reason(run: Mapping[str, Any]) -> str:
    statuses = {case["status"] for case in run["cases"]} - {"cancelled"}
    if statuses & {"wrong_output", "runtime_error"}:
        return "correctness_failure"
    if "timeout" in statuses:
        return "timeout"
    return "tool_failure"


def update_campaign_status(
    *,
    plan_path: Path,
    run_paths: Mapping[str, Path],
    study_paths: Mapping[str, Path] | None = None,
    previous_status_path: Path | None = None,
    started_at: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    plan = load_and_validate(plan_path)
    if plan["schema_version"] != "campaign-plan.v1":
        raise ValidationError("campaign status requires campaign-plan.v1")
    plan_digest = sha256_json(plan)
    previous = load_and_validate(previous_status_path) if previous_status_path is not None else None
    if previous is not None:
        compatible_plan = previous["plan_sha256"] == plan_digest or (
            plan["parent_plan_sha256"] is not None
            and previous["plan_sha256"] == plan["parent_plan_sha256"]
        )
        if previous["schema_version"] != "campaign-status.v1" or not compatible_plan:
            raise ValidationError("previous campaign status is not bound to this plan")
        if started_at is not None and started_at != previous["started_at"]:
            raise ConfigurationError("campaign started_at cannot change during resume")
        start = _parse_timestamp(previous["started_at"], label="previous started_at")
    else:
        if started_at is None:
            raise ConfigurationError("campaign started_at is required for the first status record")
        start = _parse_timestamp(started_at, label="campaign started_at")
    now = _parse_timestamp(as_of or utc_now(), label="campaign as_of")
    if now < start:
        raise ConfigurationError("campaign as_of precedes started_at")
    deadline = start + timedelta(seconds=plan["total_budget_seconds"])

    runs = _load_campaign_runs(plan, run_paths)
    for run in runs.values():
        if _parse_timestamp(run["started_at"], label="run started_at") < start:
            raise ValidationError("campaign run started before the recorded campaign start")
    decisions = _study_decisions(plan=plan, runs=runs, study_paths=study_paths or {})
    tasks = {task["task_id"]: task for task in plan["tasks"]}
    rows: list[dict[str, Any]] = []
    for task in plan["tasks"]:
        selection_state, selection_reasons = _task_selection(task, decisions)
        run = runs.get(task["task_id"])
        if run is not None and selection_state != "selected":
            raise ValidationError(f"campaign has a run for an unselected task: {task['task_id']}")
        if run is None:
            status = "not_selected" if selection_state == "not_selected" else "pending"
            missing_reason = (
                "promotion_threshold_not_met"
                if selection_state == "not_selected" and task["selection_rule"] == "smoke_promoted"
                else "top5_not_selected"
                if selection_state == "not_selected"
                else "promotion_evidence_missing"
                if selection_state == "awaiting_evidence"
                else "not_scheduled"
            )
            run_id = task["run_id"]
            run_digest = run_started = run_completed = None
        else:
            run_id = run["run_id"]
            run_digest = sha256_json(run)
            run_started = run["started_at"]
            run_completed = run["completed_at"]
            if run["state"] == "completed":
                status, missing_reason = "completed", None
            elif run["state"] == "running":
                status, missing_reason = "running", None
            elif run["state"] == "interrupted":
                status, missing_reason = "interrupted", "tool_failure"
            else:
                status, missing_reason = "failed", _failed_run_reason(run)
        rows.append(
            {
                "task_id": task["task_id"],
                "selection_state": selection_state,
                "selection_reasons": selection_reasons,
                "status": status,
                "run_id": run_id,
                "run_record_sha256": run_digest,
                "started_at": run_started,
                "completed_at": run_completed,
                "missing_reason": missing_reason,
            }
        )

    by_task = {row["task_id"]: row for row in rows}
    # Dependency classification is calculated before phase deadlines.  A
    # successful completion and correctness-diagnostic evidence are distinct
    # outcomes; no other terminal failure silently satisfies a dependency.
    for task in plan["tasks"]:
        row = by_task[task["task_id"]]
        if row["status"] != "pending" or row["selection_state"] != "selected":
            continue
        outcomes = _dependency_outcomes(task, tasks=tasks, statuses=by_task)
        if "terminal_failure" in outcomes:
            row["missing_reason"] = "dependency_failed"
        elif not _dependencies_ready(outcomes):
            row["missing_reason"] = "dependency_incomplete"

    phase_rows: list[dict[str, Any]] = []
    phase_start = start
    phase_deadlines: dict[str, datetime] = {}
    for phase in plan["phases"]:
        phase_id = phase["phase_id"]
        phase_tasks = [
            by_task[task["task_id"]] for task in plan["tasks"] if task["phase_id"] == phase_id
        ]
        awaiting = any(row["selection_state"] == "awaiting_evidence" for row in phase_tasks)
        selected = [row for row in phase_tasks if row["selection_state"] == "selected"]
        terminal = bool(selected) and not awaiting and all(
            row["status"] in {"completed", "failed", "interrupted"} for row in selected
        )
        phase_completed = (
            max(_run_end(runs[row["task_id"]]) for row in selected)
            if terminal
            else None
        )
        phase_deadline = (
            deadline
            if phase_id == "final_validation"
            else phase_start + timedelta(seconds=phase["budget_seconds"])
        )
        phase_deadlines[phase_id] = phase_deadline
        effective_budget = max(0.0, (phase_deadline - phase_start).total_seconds())
        wall_clock = max(0.0, (min(now, phase_deadline) - phase_start).total_seconds())
        if now < phase_start:
            phase_state = "pending"
        elif terminal:
            if any(row["status"] == "failed" for row in selected):
                phase_state = "failed"
            elif any(row["status"] == "interrupted" for row in selected):
                phase_state = "interrupted"
            else:
                phase_state = "completed"
        elif now >= phase_deadline:
            phase_state = "budget_exhausted"
        else:
            phase_state = "running"
        phase_rows.append(
            {
                "phase_id": phase_id,
                "state": phase_state,
                "started_at": _timestamp(phase_start),
                "deadline": _timestamp(phase_deadline),
                "completed_at": None if phase_completed is None else _timestamp(phase_completed),
                "base_budget_seconds": phase["budget_seconds"],
                "effective_budget_seconds": effective_budget,
                "wall_clock_seconds": wall_clock,
            }
        )
        phase_start = phase_completed if phase_completed is not None else phase_deadline

    # Every non-result is explicitly closed over the four public deadline
    # categories.  This includes tasks which never received the promotion
    # evidence needed to become selectable.
    for task in plan["tasks"]:
        row = by_task[task["task_id"]]
        if now < phase_deadlines[task["phase_id"]] or row["status"] == "completed":
            continue
        if row["run_record_sha256"] is None:
            if row["status"] != "not_selected":
                row["status"] = "budget_exhausted"
            row["missing_reason"] = "not_scheduled"
        elif row["status"] == "running":
            row["status"] = "budget_exhausted"
            row["missing_reason"] = "timeout"

    final_phase = next(row for row in phase_rows if row["phase_id"] == "final_validation")
    transferred = max(0.0, final_phase["effective_budget_seconds"] - 12 * 60 * 60)
    elapsed = min(float(plan["total_budget_seconds"]), max(0.0, (now - start).total_seconds()))
    remaining = max(0.0, (deadline - now).total_seconds())
    selected_rows = [row for row in rows if row["selection_state"] == "selected"]
    phase_terminal = all(
        row["state"] in {"completed", "failed", "interrupted"}
        for row in phase_rows
    )
    if selected_rows and all(row["status"] == "completed" for row in selected_rows) and decisions["final_pair_coverage_complete"]:
        state = "completed"
    elif now >= deadline:
        state = "budget_exhausted"
    elif phase_terminal and decisions["final_pair_coverage_complete"]:
        if any(row["status"] == "failed" for row in selected_rows):
            state = "failed"
        elif any(row["status"] == "interrupted" for row in selected_rows):
            state = "interrupted"
        else:
            state = "completed"
    elif runs:
        state = "running"
    else:
        state = "pending"
    return validate_document(
        {
            "schema_version": "campaign-status.v1",
            "campaign_id": plan["campaign_id"],
            "plan_sha256": plan_digest,
            "state": state,
            "started_at": _timestamp(start),
            "as_of": _timestamp(now),
            "deadline": _timestamp(deadline),
            "elapsed_wall_clock_seconds": elapsed,
            "remaining_wall_clock_seconds": remaining,
            "unused_budget_transferred_to_final_seconds": transferred,
            "promotion_decisions": decisions,
            "phases": phase_rows,
            "tasks": rows,
        }
    )


def next_campaign_tasks(plan: Mapping[str, Any], status: Mapping[str, Any]) -> list[dict[str, Any]]:
    if status["plan_sha256"] != sha256_json(plan) or status["campaign_id"] != plan["campaign_id"]:
        raise ValidationError("campaign status does not belong to the supplied plan")
    if status["remaining_wall_clock_seconds"] <= 0:
        return []
    tasks_by_id = {task["task_id"]: task for task in plan["tasks"]}
    by_id = {row["task_id"]: row for row in status["tasks"]}
    phase_by_id = {row["phase_id"]: row for row in status["phases"]}
    ready: list[dict[str, Any]] = []
    for task in plan["tasks"]:
        row = by_id[task["task_id"]]
        if row["status"] != "pending" or row["selection_state"] != "selected":
            continue
        if not _dependencies_ready(
            _dependency_outcomes(task, tasks=tasks_by_id, statuses=by_id)
        ):
            continue
        phase = phase_by_id[task["phase_id"]]
        if phase["state"] != "running":
            continue
        ready.append(
            {
                key: task[key]
                for key in (
                    "task_id",
                    "suite_role",
                    "suite_id",
                    "manifest_sha256",
                    "profile_id",
                    "profile_sha256",
                    "profile_path",
                    "measurement_mode",
                    "required_evidence_level",
                    "run_id",
                    "compiler_baseline",
                    "oracle_leg",
                )
            }
            | {"phase_deadline": phase["deadline"]}
        )
    return ready[: plan["max_workers"]]


def campaign_task(
    plan: Mapping[str, Any], *, task_id: str, field: str | None = None
) -> Any:
    if plan.get("schema_version") != "campaign-plan.v1":
        raise ValidationError("campaign task query requires campaign-plan.v1")
    matches = [task for task in plan["tasks"] if task["task_id"] == task_id]
    if len(matches) != 1:
        raise ConfigurationError(f"campaign task id is not present exactly once: {task_id}")
    task = deepcopy(matches[0])
    if field is None:
        return task
    if field not in task:
        raise ConfigurationError(f"unknown campaign task field: {field}")
    return task[field]
