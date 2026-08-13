from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError, ValidationError
from .analyzer_contract import candidate_analyzer_contract
from .fast_campaign import (
    FAST_ORACLE_STATIC_ARTIFACT_VERSIONS,
    build_fast_campaign_plan,
    fast_configuration_template_sha256,
    verify_fast_oracle_static_artifacts,
)
from .journal import durable_create_json
from .schema import load_and_validate, validate_pipeline_profile_v2
from .metrics import cache_hotblock_metrics_v1, rv64gc_qemu_v1
from .util import (
    canonical_json_bytes,
    read_json,
    safe_slug,
    sha256_artifact,
    sha256_json,
    validate_relative_path,
)


_BLUEPRINT_VERSION = "candidate-fast-launch-blueprints.v1"
_TEMPLATE_VERSION = "candidate-fast-launch-templates.v1"
_RUN_SELECTORS = {
    "accela-standard",
    "accela-cache",
    "reference-gcc",
    "reference-clang",
}
_CONFIGURATION_KEYS = {
    "compiler", "pipeline_profile_file_sha256", "candidate_registry_sha256",
    "candidate_pass_registry_sha256", "enabled_candidate_ids", "linker", "analyzer",
    "runner", "primary_metric_id", "metric_profile_id", "metrics",
    "compile_timeout_seconds", "compile_repetitions", "reuse_compile_cache",
    "compile_storage_contract", "link_timeout_seconds", "analyze_timeout_seconds",
    "run_timeout_seconds", "timeout_policy", "baseline_timeout_run_sha256",
    "baseline_timeout_run_id", "timeout_minimum_seconds", "timeout_multiplier",
    "timeout_cap_seconds", "repetitions", "max_workers", "keep_going",
    "retry_failures", "seed", "artifact_suffix", "binary_suffix",
    "wsl_distribution_sha256", "metric_file_sha256", "analysis_file_sha256",
    "remarks_file_sha256", "result_file_sha256", "output_contract",
    "environment_label", "evidence_level", "tool_versions",
    "consistency_fraction", "consistency_repetitions",
}
_PROVENANCE_KEYS = {
    "repo_commit", "repo_dirty", "tracked_diff_sha256", "pipeline_profile_id",
    "pipeline_profile_sha256", "compiler_artifact_sha256",
    "execution_environment_sha256", "measurement_protocol_id",
    "measurement_protocol_sha256",
}
_CONTROLLED_OPTIONS = {
    "--workspace-root",
    "--output",
    "--state-dir",
    "--run-id",
    "--repo-commit",
    "--repo-dirty",
    "--tracked-diff-sha256",
    "--pipeline-profile-id",
    "--pipeline-profile-file",
    "--pipeline-profile-sha256",
    "--candidate-registry",
    "--candidate-pass-registry",
    "--compiler-artifact",
    "--execution-environment-sha256",
    "--measurement-protocol",
    "--baseline-timeout-run",
    "--timeout-policy",
    "--candidate-campaign-plan",
    "--candidate-campaign-status",
    "--candidate-status-ledger",
    "--candidate-task-id",
    "--candidate-fast-plan",
    "--candidate-fast-status",
    "--candidate-fast-index",
    "--candidate-fast-task-id",
    "--candidate-fast-receipt",
    "--compile-repetitions",
    "--reuse-compile-cache",
    "--repetitions",
    "--jobs",
    "--keep-going",
    "--retry-failures",
    "--seed",
    "--metric-profile",
    "--metric-extension",
    "--environment-label",
    "--evidence-level",
    "--output-contract",
}


def _root(path: Path) -> Path:
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ConfigurationError("fast plan workspace_root must be a directory")
    return root


def _inside(root: Path, value: str | Path, *, label: str, exists: bool) -> tuple[Path, str]:
    candidate = Path(value)
    physical = (candidate if candidate.is_absolute() else root / candidate).absolute()
    try:
        relative = physical.relative_to(root).as_posix()
    except ValueError as exc:
        raise ConfigurationError(f"{label} must stay within workspace_root") from exc
    validate_relative_path(relative, label=label)
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValidationError(f"{label} must not traverse a symbolic link")
    if exists and (not physical.exists() or not physical.is_file()):
        raise ConfigurationError(f"{label} must be an existing regular file")
    return physical, relative


def _artifact(root: Path, value: str | Path, *, label: str) -> dict[str, str]:
    physical, relative = _inside(root, value, label=label, exists=False)
    if not physical.exists() or (not physical.is_file() and not physical.is_dir()):
        raise ConfigurationError(f"{label} must be an existing file or directory artifact")
    digest = sha256_artifact(physical)
    canonical = digest
    if physical.is_file() and physical.suffix.lower() == ".json":
        document = read_json(physical)
        canonical = sha256_json(document)
    return {
        "path": relative,
        "canonical_sha256": canonical,
        "physical_sha256": digest,
    }


def _verify_artifact(root: Path, row: Mapping[str, Any], *, label: str) -> Path:
    observed = _artifact(root, str(row["path"]), label=label)
    if observed != dict(row):
        raise ValidationError(f"{label} hash binding differs")
    return root / observed["path"]


def _publish_immutable(path: Path, document: Mapping[str, Any], *, label: str) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    if path.is_symlink():
        raise ValidationError(f"{label} must not be a symbolic link")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValidationError(f"{label} already exists with different bytes")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_create_json(path, document)


def _load_blueprints(root: Path, path: Path) -> dict[str, dict[str, Any]]:
    physical, _ = _inside(root, path, label="fast launch blueprints", exists=True)
    document = read_json(physical)
    return _validate_blueprint_document(root, document)


def _validate_blueprint_document(
    root: Path, document: Any
) -> dict[str, dict[str, Any]]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "blueprints", "blueprint_commitment_sha256"
    }:
        raise ValidationError("fast launch blueprints have an invalid top-level contract")
    if document["schema_version"] != _BLUEPRINT_VERSION:
        raise ValidationError("fast launch blueprint schema version differs")
    if document["blueprint_commitment_sha256"] != sha256_json(
        {key: value for key, value in document.items() if key != "blueprint_commitment_sha256"}
    ):
        raise ValidationError("fast launch blueprint commitment differs")
    rows = document["blueprints"]
    if not isinstance(rows, list) or len(rows) != len(_RUN_SELECTORS):
        raise ValidationError("fast launch blueprints require four execution selectors")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "selector", "compiler_artifact", "argv_tail", "configuration", "provenance"
        }:
            raise ValidationError("fast launch blueprint fields differ")
        selector = row["selector"]
        if selector not in _RUN_SELECTORS or selector in result:
            raise ValidationError("fast launch blueprint selector set differs")
        artifact = row["compiler_artifact"]
        if not isinstance(artifact, dict):
            raise ValidationError("fast launch blueprint compiler artifact is invalid")
        _verify_artifact(root, artifact, label=f"fast {selector} compiler")
        argv_tail = row["argv_tail"]
        if (
            not isinstance(argv_tail, list)
            or not argv_tail
            or any(not isinstance(item, str) or not item for item in argv_tail)
            or any(item in _CONTROLLED_OPTIONS for item in argv_tail)
            or any("{baseline" in item or "{status" in item or "{index" in item for item in argv_tail)
        ):
            raise ValidationError("fast launch blueprint argv tail contains controlled arguments")
        configuration = row["configuration"]
        provenance = row["provenance"]
        if not isinstance(configuration, dict) or not isinstance(provenance, dict):
            raise ValidationError("fast launch blueprint configuration/provenance is invalid")
        if set(configuration) != _CONFIGURATION_KEYS or set(provenance) != _PROVENANCE_KEYS:
            raise ValidationError("fast launch blueprint is not a complete run configuration")
        fixed = {
            "compile_timeout_seconds": 120.0,
            "compile_repetitions": 5,
            "reuse_compile_cache": False,
            "compile_storage_contract": "attempt_local_v1",
            "link_timeout_seconds": 120.0,
            "analyze_timeout_seconds": 120.0,
            "run_timeout_seconds": 1800.0,
            "timeout_minimum_seconds": 120.0,
            "timeout_multiplier": 3.0,
            "timeout_cap_seconds": 1800.0,
            "repetitions": 1,
            "max_workers": 4,
            "keep_going": False,
            "retry_failures": False,
            "seed": 20260809,
            "artifact_suffix": ".s",
            "binary_suffix": ".elf",
            "output_contract": "lf_return_trailer",
            "environment_label": "proxy",
            "evidence_level": "qemu_proxy",
            "consistency_fraction": 0.1,
            "consistency_repetitions": 3,
            "metric_profile_id": "rv64gc-qemu-v1",
            "primary_metric_id": "dynamic_instruction_count",
        }
        if any(configuration.get(key) != value for key, value in fixed.items()):
            raise ValidationError("fast launch blueprint differs from the fixed formal run contract")
        option_names = {item for item in argv_tail if item.startswith("--")}
        required_options = {
            "--compiler-command-json", "--link-command-json",
            "--analyzer-command-json", "--runner-command-json",
            "--metric-file", "--analysis-file",
            "--measurement-asset", "--tool-version", "--official-version",
        }
        if not selector.startswith("reference-"):
            required_options.add("--remarks-file")
        if not required_options <= option_names:
            raise ValidationError("fast launch blueprint omits formal command or evidence arguments")
        result[selector] = dict(row)
    if set(result) != _RUN_SELECTORS:
        raise ValidationError("fast launch blueprint selector set is incomplete")
    return result


def _named(artifact_id: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {"artifact_id": artifact_id, "artifact": dict(artifact)}


def _stage(
    kind: str,
    argv: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment_values = {} if environment is None else dict(environment)
    return {
        "kind": kind,
        "adapter": "host",
        "command_sha256": sha256_json(
            {"command": list(argv), "environment": environment_values}
        ),
        "executable": Path(argv[0]).name,
        "environment_keys": sorted(environment_values),
    }


def _metric_rows(*, cache: bool) -> list[dict[str, Any]]:
    preset = rv64gc_qemu_v1()
    rows = [
        {
            "metric_id": preset["primary_metric_id"],
            "source": preset["metric_source"],
            "pattern_sha256": sha256_json(preset["metric_pattern"]),
            "unit": preset["metric_unit"],
        }
    ]
    for row in [*preset["additional"], *(cache_hotblock_metrics_v1() if cache else [])]:
        rows.append(
            {
                "metric_id": row["metric_id"],
                "source": row["source"],
                "pattern_sha256": None if row["pattern"] is None else sha256_json(row["pattern"]),
                "unit": row["unit"],
            }
        )
    return rows


def build_fast_launch_blueprints(
    *, workspace_root: Path, bootstrap_path: Path, source_plan_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Derive the four complete launch shapes from frozen normalized evidence."""

    root = _root(workspace_root)
    bootstrap_physical, _ = _inside(root, bootstrap_path, label="fast bootstrap", exists=True)
    source_physical, _ = _inside(root, source_plan_path, label="source campaign plan", exists=True)
    bootstrap = load_and_validate(bootstrap_physical)
    source = load_and_validate(source_physical)
    if bootstrap.get("schema_version") != "candidate-fast-bootstrap.v1" or source.get("schema_version") != "candidate-campaign-plan.v1":
        raise ValidationError("fast blueprint producer requires validated bootstrap/source plan")
    source_binding = next(
        (row["artifact"] for row in bootstrap["source_artifacts"] if row["artifact_id"] == "source-plan"),
        None,
    )
    if source_binding != _artifact(root, source_physical, label="source campaign plan"):
        raise ValidationError("fast blueprint source plan differs from bootstrap")
    imported = {row["task_id"]: row for row in bootstrap["imported_receipts"]}
    b2 = imported.get("run.B2.full")
    if b2 is None:
        raise ValidationError("fast blueprint producer requires imported B2 FULL")
    b2_path = _verify_artifact(root, b2["run_artifact"], label="fast imported B2 FULL")
    b2_run = load_and_validate(b2_path)
    if (
        b2_run.get("schema_version") != "run-record.v1"
        or b2_run.get("state") != "completed"
        or b2_run.get("run_id") != b2["run_id"]
        or any(case.get("status") != "passed" for case in b2_run.get("cases", []))
    ):
        raise ValidationError("fast blueprint B2 FULL is not a completed normalized run")
    base_configuration = dict(b2_run["configuration"])
    base_provenance = dict(b2_run["provenance"])
    environment_sha256 = source["execution_environment_sha256"]
    if base_provenance.get("execution_environment_sha256") != environment_sha256:
        raise ValidationError("fast blueprint B2 FULL environment differs from source plan")

    snapshot = source["reference_toolchain"]["snapshot"]
    _verify_artifact(root, snapshot, label="reference toolchain snapshot")
    for mode, protocol in source["measurement_protocols"].items():
        _verify_artifact(
            root,
            {
                "path": protocol["path"],
                "canonical_sha256": protocol["protocol_sha256"],
                "physical_sha256": protocol["physical_sha256"],
            },
            label=f"source {mode} measurement protocol",
        )
    compiler = source["repository"]["compiler_artifact"]
    compiler_path, compiler_relative = _inside(
        root, compiler["path"], label="ACCELA compiler artifact", exists=False
    )
    if not compiler_path.exists():
        raise ValidationError("ACCELA compiler artifact is missing")
    compiler_digest = sha256_artifact(compiler_path)
    if compiler_digest != compiler["physical_sha256"]:
        raise ValidationError("ACCELA compiler artifact hash differs")
    compiler_artifact = {
        "path": compiler_relative,
        "canonical_sha256": compiler_digest,
        "physical_sha256": compiler_digest,
    }
    analyzer = candidate_analyzer_contract()
    common_versions = source["reference_toolchain"]["common_tool_versions"]
    analyzer_by_selector = {
        "accela-standard": "accela", "accela-cache": "accela",
        "reference-gcc": "gcc", "reference-clang": "clang",
    }
    protocol_by_selector = {
        "accela-standard": "standard_proxy", "reference-gcc": "standard_proxy",
        "reference-clang": "standard_proxy", "accela-cache": "cache_hotblock",
    }
    baseline_by_selector = {
        "reference-gcc": next(row for row in source["reference_toolchain"]["baselines"] if row["profile_id"] == "gcc-13.3-o2"),
        "reference-clang": next(row for row in source["reference_toolchain"]["baselines"] if row["profile_id"] == "clang-18-o3"),
    }
    rows: list[dict[str, Any]] = []
    for selector in sorted(_RUN_SELECTORS):
        cache = selector == "accela-cache"
        reference = selector.startswith("reference-")
        protocol = source["measurement_protocols"][protocol_by_selector[selector]]
        runner_path = "scripts/benchmark-qemu-hotblocks.sh" if cache else "scripts/benchmark-qemu.sh"
        runner_environment = {
            "QEMU_SYSTEM_RISCV64": "{qemu_binary}",
            "QEMU_PROFILE_PLUGIN": "{profile_plugin_binary}",
            "QEMU_CACHE_PLUGIN": "{cache_plugin_binary}",
            **({"QEMU_HOTBLOCK_PLUGIN": "{hotblocks_plugin_binary}"} if cache else {}),
        }
        runner_argv = ["sh", "{runner_executable}", "{binary}", "{metric_file}", "{input}"]
        runner_stage = _stage("qemu", runner_argv, environment=runner_environment)
        if runner_stage["command_sha256"] != protocol["runner_command_sha256"]:
            raise ValidationError(f"fast {selector} runner command differs from source protocol")
        analyzer_argv = analyzer["commands"][analyzer_by_selector[selector]]["argv"]
        if reference:
            baseline = baseline_by_selector[selector]
            frontend = "gcc" if selector == "reference-gcc" else "clang"
            compiler_argv = ["sh", "scripts/reference-compile.sh", frontend, "{source}", "{artifact}"]
            selected_compiler = snapshot
            tool_versions = {**common_versions, baseline["tool"]: baseline["version"]}
        else:
            compiler_argv = ["sh", "scripts/benchmark-compile.sh", "{profile}", "{source}", "{artifact}", "{remarks_file}"]
            selected_compiler = compiler_artifact
            tool_versions = {**common_versions, "accela-jdk": source["reference_toolchain"]["accela_jdk_version"]}
        linker_argv = ["sh", "scripts/benchmark-link.sh", "{artifact}", "{binary}"]
        configuration = dict(base_configuration)
        configuration.update(
            {
                "pipeline_profile_file_sha256": (
                    None
                    if reference
                    else base_configuration["pipeline_profile_file_sha256"]
                ),
                "candidate_registry_sha256": (
                    None
                    if reference
                    else source["artifacts"]["candidate_registry"]["canonical_sha256"]
                ),
                "candidate_pass_registry_sha256": (
                    None
                    if reference
                    else source["artifacts"]["executable_pass_registry"]["canonical_sha256"]
                ),
                "enabled_candidate_ids": [],
                "compiler": _stage("external" if reference else "benchmark-compiler", compiler_argv),
                "linker": _stage("external", linker_argv),
                "analyzer": _stage("analyzer", analyzer_argv),
                "runner": runner_stage,
                "primary_metric_id": "dynamic_instruction_count", "metric_profile_id": "rv64gc-qemu-v1",
                "metrics": _metric_rows(cache=cache), "compile_timeout_seconds": 120.0,
                "compile_repetitions": 5, "reuse_compile_cache": False,
                "compile_storage_contract": "attempt_local_v1", "link_timeout_seconds": 120.0,
                "analyze_timeout_seconds": 120.0, "run_timeout_seconds": 1800.0,
                "timeout_policy": "initial", "baseline_timeout_run_sha256": None,
                "baseline_timeout_run_id": None, "timeout_minimum_seconds": 120.0,
                "timeout_multiplier": 3.0, "timeout_cap_seconds": 1800.0,
                "repetitions": 1, "max_workers": 4, "keep_going": False,
                "retry_failures": False, "seed": 20260809, "artifact_suffix": ".s",
                "binary_suffix": ".elf", "metric_file_sha256": sha256_json("metrics.log"),
                "analysis_file_sha256": sha256_json("binary-analysis.json"),
                "remarks_file_sha256": None if reference else sha256_json("optimization-remarks.jsonl"),
                "result_file_sha256": None, "output_contract": "lf_return_trailer",
                "environment_label": "proxy", "evidence_level": "qemu_proxy",
                "consistency_fraction": 0.1, "consistency_repetitions": 3,
                "tool_versions": [
                    {"tool": key, "actual": value, "official_expected": value, "comparison": "exact"}
                    for key, value in sorted(tool_versions.items())
                ],
            }
        )
        provenance = dict(base_provenance)
        reference_profile = baseline_by_selector.get(selector)
        provenance.update(
            {
                "repo_commit": bootstrap["evaluation_revision"]["commit"],
                "repo_dirty": False, "tracked_diff_sha256": None,
                "compiler_artifact_sha256": selected_compiler["physical_sha256"],
                "execution_environment_sha256": environment_sha256,
                "measurement_protocol_id": protocol["protocol_id"],
                "measurement_protocol_sha256": protocol["protocol_sha256"],
                "pipeline_profile_id": (
                    reference_profile["profile_id"]
                    if reference_profile is not None
                    else base_provenance["pipeline_profile_id"]
                ),
                "pipeline_profile_sha256": (
                    reference_profile["profile_sha256"]
                    if reference_profile is not None
                    else base_provenance["pipeline_profile_sha256"]
                ),
            }
        )
        assets = [
            binding for binding in bootstrap["static_artifacts"]
            if binding["artifact_id"] in {
                "profile_plugin_source", "cache_plugin_source", "hotblocks_plugin_source",
                "runtime_filter_source", "runtime_source", "crt_source", "linker_script_source",
                "profile_plugin_binary", "cache_plugin_binary", "hotblocks_plugin_binary",
                "qemu_binary", "runner_executable_standard" if not cache else "runner_executable_cache",
            }
        ]
        asset_ids = {row["artifact_id"] for row in assets}
        runner_asset_id = "runner_executable_cache" if cache else "runner_executable_standard"
        required_assets = {
            "profile_plugin_source", "cache_plugin_source", "hotblocks_plugin_source",
            "runtime_filter_source", "runtime_source", "crt_source", "linker_script_source",
            "profile_plugin_binary", "cache_plugin_binary", "hotblocks_plugin_binary",
            "qemu_binary", runner_asset_id,
        }
        if asset_ids != required_assets:
            raise ValidationError(f"fast {selector} bootstrap measurement asset set differs")
        for item in assets:
            _verify_artifact(
                root,
                item["artifact"],
                label=f"fast {selector} measurement asset {item['artifact_id']}",
            )
        argv_tail = [
            "--compiler-kind", "external" if reference else "benchmark-compiler",
            "--compiler-command-json", json.dumps(compiler_argv, separators=(",", ":")),
            "--link-command-json", json.dumps(linker_argv, separators=(",", ":")),
            "--analyzer-command-json", json.dumps(analyzer_argv, separators=(",", ":")),
            "--analysis-file", "binary-analysis.json",
            "--runner-command-json", json.dumps(runner_argv, separators=(",", ":")),
            *sum((["--runner-env", f"{key}={value}"] for key, value in sorted(runner_environment.items())), []),
            "--metric-file", "metrics.log",
            *( [] if reference else ["--remarks-file", "optimization-remarks.jsonl"] ),
            *sum(
                (
                    [
                        "--measurement-asset",
                        f"{('runner_executable' if item['artifact_id'].startswith('runner_executable') else item['artifact_id'])}={item['artifact']['path']}",
                    ]
                    for item in assets
                ),
                [],
            ),
            *sum((["--tool-version", f"{key}={value}", "--official-version", f"{key}={value}"] for key, value in sorted(tool_versions.items())), []),
        ]
        rows.append(
            {
                "selector": selector, "compiler_artifact": dict(selected_compiler),
                "argv_tail": argv_tail, "configuration": configuration,
                "provenance": provenance,
            }
        )
    document: dict[str, Any] = {
        "schema_version": _BLUEPRINT_VERSION, "blueprints": rows,
        "blueprint_commitment_sha256": "0" * 64,
    }
    document["blueprint_commitment_sha256"] = sha256_json(
        {key: value for key, value in document.items() if key != "blueprint_commitment_sha256"}
    )
    _validate_blueprint_document(root, document)
    output, _ = _inside(root, output_path, label="fast blueprint output", exists=False)
    _publish_immutable(output, document, label="fast launch blueprints")
    return document


def _profile_document(candidate_ids: Sequence[str], candidate_order: Sequence[str]) -> dict[str, Any]:
    selected = set(candidate_ids)
    return validate_pipeline_profile_v2(
        {
            "schema_version": 2,
            "base": "FULL",
            "disable": [],
            "enable_candidates": [item for item in candidate_order if item in selected],
        },
        candidate_order=list(candidate_order),
    )


def _pseudo_task(
    *, ordinal: int, task_id: str, kind: str, stage: str, dependencies: Sequence[str],
    terminal_dependencies: Sequence[str] = (), gate: str = "dependencies_succeeded",
    static_bindings: Sequence[Mapping[str, Any]], output_path: str,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "task_id": task_id,
        "kind": kind,
        "run_kind": None,
        "stage": stage,
        "candidate_ids": [],
        "data_role": stage,
        "measurement_mode": "none",
        "dependencies": list(dependencies),
        "terminal_dependencies": list(terminal_dependencies),
        "gate": gate,
        "static_bindings": [dict(item) for item in static_bindings],
        "output_path": output_path,
        "receipt_path": None,
        "run_id": None,
        "logical_profile_id": None,
        "expected_configuration_template_sha256": None,
        "reference_profile_id": None,
        "reference_profile_sha256": None,
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


def build_fast_plan_factory(
    *,
    workspace_root: Path,
    bootstrap_path: Path,
    source_plan_path: Path,
    blueprint_path: Path,
    plan_id: str,
    plan_output_path: Path,
    launch_template_output_path: Path,
    campaign_output_root: Path,
    campaign_state_root: Path,
    diagnostic_profile_root: Path,
    created_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate the complete fast-v2 DAG and immutable launch templates.

    The source plan supplies all frozen suites, registries, protocols, toolchain and
    execution-environment identities.  Blueprints contain only the four reusable
    command/configuration shapes; no task row or dependency is accepted as input.
    """

    root = _root(workspace_root)
    bootstrap_physical, _ = _inside(root, bootstrap_path, label="fast bootstrap", exists=True)
    source_physical, _ = _inside(root, source_plan_path, label="source campaign plan", exists=True)
    bootstrap = load_and_validate(bootstrap_physical)
    source = load_and_validate(source_physical)
    if bootstrap.get("schema_version") != "candidate-fast-bootstrap.v1":
        raise ValidationError("fast plan factory requires candidate-fast-bootstrap.v1")
    if source.get("schema_version") != "candidate-campaign-plan.v1":
        raise ValidationError("fast plan factory requires candidate-campaign-plan.v1")
    candidates = list(source["qualified_candidate_ids"])
    if candidates != list(dict.fromkeys(candidates)):
        raise ValidationError("fast source candidate identity is not ordered and unique")
    source_plan_binding = next(
        (
            row["artifact"]
            for row in bootstrap["source_artifacts"]
            if row["artifact_id"] == "source-plan"
        ),
        None,
    )
    observed_source_artifact = _artifact(root, source_physical, label="source campaign plan")
    if source_plan_binding != observed_source_artifact:
        raise ValidationError("fast bootstrap does not bind the selected source plan")
    if (
        source["repository"]["repo_commit"] != bootstrap["source_revision"]["commit"]
        or source["repository"]["repo_tree"] != bootstrap["source_revision"]["tree"]
        or source["repository"]["compiler_artifact"]["physical_sha256"]
        == "0" * 64
    ):
        raise ValidationError("fast bootstrap/source revision identity differs")
    if source["execution_environment_sha256"] == "0" * 64:
        raise ValidationError("fast source execution environment is a placeholder")
    if bootstrap["evaluation_revision"]["dirty"]:
        raise ValidationError("fast evaluation revision must be clean")
    oracle_bundle = verify_fast_oracle_static_artifacts(
        workspace_root=root,
        named_artifacts=bootstrap["static_artifacts"],
    )

    blueprints = _load_blueprints(root, blueprint_path)
    for selector, blueprint in blueprints.items():
        provenance = blueprint["provenance"]
        if provenance.get("execution_environment_sha256") != source["execution_environment_sha256"]:
            raise ValidationError(f"fast {selector} execution environment differs from source plan")
        if (
            provenance.get("compiler_artifact_sha256")
            != blueprint["compiler_artifact"]["physical_sha256"]
        ):
            raise ValidationError(f"fast {selector} compiler provenance differs")
        configuration = blueprint["configuration"]
        if (
            configuration["tool_versions"] != sorted(
                configuration["tool_versions"], key=lambda row: row["tool"]
            )
            or any(row["comparison"] != "exact" for row in configuration["tool_versions"])
            or configuration["compiler"] is None
            or configuration["linker"] is None
            or configuration["analyzer"] is None
            or configuration["runner"] is None
        ):
            raise ValidationError(f"fast {selector} stage/tool-version contract differs")
        if selector.startswith("reference-") and any(
            "{profile}" in item or item == "--pipeline-profile-file"
            for item in blueprint["argv_tail"]
        ):
            raise ValidationError("fast reference blueprint must not consume an ACCELA profile")

    _, output_root = _inside(root, campaign_output_root, label="fast campaign output root", exists=False)
    _, state_root = _inside(root, campaign_state_root, label="fast campaign state root", exists=False)
    profile_directory, profile_root = _inside(
        root, diagnostic_profile_root, label="fast diagnostic profile root", exists=False
    )
    plan_physical, plan_relative = _inside(root, plan_output_path, label="fast plan output", exists=False)
    launch_physical, _ = _inside(
        root, launch_template_output_path, label="fast launch template output", exists=False
    )

    source_artifact = observed_source_artifact
    static = [_named("source-campaign-plan", source_artifact)]
    for artifact_id, artifact in source["artifacts"].items():
        _verify_artifact(root, artifact, label=f"source {artifact_id}")
        if artifact_id == "screening":
            if artifact != oracle_bundle["candidate-screening"]["artifact"]:
                raise ValidationError(
                    "fast source plan screening differs from the bootstrap Oracle closure"
                )
            continue
        static.append(_named(artifact_id.replace("_", "-"), artifact))
    static.extend(
        _named(artifact_id, oracle_bundle[artifact_id]["artifact"])
        for artifact_id in FAST_ORACLE_STATIC_ARTIFACT_VERSIONS
    )
    snapshot = source["reference_toolchain"]["snapshot"]
    _verify_artifact(root, snapshot, label="source reference toolchain snapshot")
    static.append(_named("reference-toolchain-snapshot", snapshot))

    suites = {row["data_role"]: row for row in source["suites"]}
    if set(suites) != {"B1", "B2", "B3", "B4", "B5", "B6"}:
        raise ValidationError("source campaign plan must bind exactly B1-B6")
    protocols: dict[str, dict[str, str]] = {}
    for mode in ("standard_proxy", "cache_hotblock"):
        contract = source["measurement_protocols"][mode]
        artifact = {
            "path": contract["path"],
            "canonical_sha256": contract["protocol_sha256"],
            "physical_sha256": contract["physical_sha256"],
        }
        _verify_artifact(root, artifact, label=f"source {mode} protocol")
        protocols[mode] = artifact

    matrix_artifact = source["artifacts"]["matrix"]
    matrix_path = _verify_artifact(root, matrix_artifact, label="source profile matrix")
    matrix = load_and_validate(matrix_path)
    profiles: dict[tuple[str, ...], tuple[str, dict[str, str]]] = {}
    for row in matrix["profiles"]:
        artifact = _artifact(root, row["path"], label=f"profile {row['profile_id']}")
        if artifact["physical_sha256"] != row["profile_sha256"]:
            raise ValidationError(f"profile physical hash differs: {row['profile_id']}")
        profiles[tuple(row["candidate_ids"])] = (row["profile_id"], artifact)
    if () not in profiles or any((candidate,) not in profiles for candidate in candidates):
        raise ValidationError("source matrix lacks the exact candidate-empty/single profile set")

    for left, right in combinations(candidates, 2):
        selected = tuple(sorted((left, right)))
        profile_id = "full+" + "+".join(selected)
        profile_path = profile_directory / (safe_slug(profile_id) + ".json")
        profile = _profile_document(selected, candidates)
        _publish_immutable(profile_path, profile, label=f"fast diagnostic profile {profile_id}")
        profiles[selected] = (profile_id, _artifact(root, profile_path, label=profile_id))
    reference_profiles = {
        row["profile_id"]: row["profile_sha256"]
        for row in source["reference_toolchain"]["baselines"]
    }
    if set(reference_profiles) != {"gcc-13.3-o2", "clang-18-o3"} or any(
        value == "0" * 64 for value in reference_profiles.values()
    ):
        raise ValidationError("source reference profile identity set differs")

    imported = {row["task_id"]: row for row in bootstrap["imported_receipts"]}
    required_imports = {"run.B1.full", "run.B2.full", *{f"run.B1.{item}" for item in candidates}}
    if set(imported) != required_imports:
        raise ValidationError("fast bootstrap must contain exactly B1 FULL/singles and B2 FULL")
    b2_baseline = imported["run.B2.full"]["run_artifact"]
    _verify_artifact(root, b2_baseline, label="fast imported B2 FULL")

    tasks: list[dict[str, Any]] = []
    launch_rows: list[dict[str, Any]] = []

    def add_pseudo(
        task_id: str, kind: str, stage: str, dependencies: Sequence[str],
        *, terminal: Sequence[str] = (), gate: str = "dependencies_succeeded",
    ) -> None:
        tasks.append(
            _pseudo_task(
                ordinal=len(tasks), task_id=task_id, kind=kind, stage=stage,
                dependencies=dependencies, terminal_dependencies=terminal, gate=gate,
                static_bindings=static,
                output_path=f"{output_root}/{'final' if kind == 'final' else kind + 's'}/{safe_slug(task_id)}.json",
            )
        )

    def add_run(
        task_id: str, *, kind: str, stage: str, selected: Sequence[str],
        mode: str, dependencies: Sequence[str], gate: str,
        baseline_task_id: str | None, baseline_artifact: Mapping[str, Any] | None,
        ranking: bool, selector: str, reference_profile_id: str | None = None,
    ) -> None:
        suite = suites["B3" if stage == "diagnostic" else stage]
        protocol = protocols["cache_hotblock" if mode == "cache_hotblock" else "standard_proxy"]
        profile_id, candidate_profile = profiles[tuple(selected)]
        profile = None if reference_profile_id is not None else candidate_profile
        reference_profile_sha256 = (
            None if reference_profile_id is None else reference_profiles[reference_profile_id]
        )
        logical_id = reference_profile_id or profile_id
        blueprint = blueprints[selector]
        compiler_artifact = (
            snapshot if reference_profile_id is not None else blueprint["compiler_artifact"]
        )
        if reference_profile_id is not None and blueprint["compiler_artifact"] != snapshot:
            raise ValidationError("fast reference blueprint compiler must bind the frozen toolchain snapshot")
        configuration = dict(blueprint["configuration"])
        configuration.update(
            {
                "pipeline_profile_file_sha256": (
                    None if profile is None else profile["physical_sha256"]
                ),
                "candidate_registry_sha256": (
                    None
                    if reference_profile_id is not None
                    else source["artifacts"]["candidate_registry"]["canonical_sha256"]
                ),
                "candidate_pass_registry_sha256": (
                    None
                    if reference_profile_id is not None
                    else source["artifacts"]["executable_pass_registry"]["canonical_sha256"]
                ),
                "enabled_candidate_ids": list(selected),
                "compile_repetitions": 5,
                "reuse_compile_cache": False,
                "repetitions": 1,
                "max_workers": 4,
                "keep_going": False,
                "retry_failures": False,
                "seed": 20260809,
                "timeout_policy": "initial" if baseline_task_id is None else "baseline_derived",
                "baseline_timeout_run_id": None,
                "baseline_timeout_run_sha256": None,
                "consistency_fraction": 0.1,
                "consistency_repetitions": 3,
            }
        )
        provenance = dict(blueprint["provenance"])
        provenance.update(
            {
                "repo_commit": bootstrap["evaluation_revision"]["commit"],
                "repo_dirty": False,
                "tracked_diff_sha256": None,
                "pipeline_profile_id": logical_id,
                "pipeline_profile_sha256": (
                    reference_profile_sha256
                    if reference_profile_sha256 is not None
                    else profile["physical_sha256"]
                ),
                "compiler_artifact_sha256": compiler_artifact["physical_sha256"],
                "execution_environment_sha256": source["execution_environment_sha256"],
                "measurement_protocol_id": source["measurement_protocols"][
                    "cache_hotblock" if mode == "cache_hotblock" else "standard_proxy"
                ]["protocol_id"],
                "measurement_protocol_sha256": protocol["canonical_sha256"],
            }
        )
        output = f"{output_root}/runs/{safe_slug(task_id)}.json"
        receipt = f"{output_root}/receipts/{safe_slug(task_id)}.json"
        run_id = f"{bootstrap['campaign_id']}:{task_id}"
        task = {
            "ordinal": len(tasks), "task_id": task_id, "kind": kind,
            "run_kind": (None if kind == "diagnostic" else ("reference" if reference_profile_id else ("candidate_empty" if not selected else "single"))),
            "stage": stage, "candidate_ids": list(selected),
            "data_role": "B3" if stage == "diagnostic" else stage,
            "measurement_mode": mode, "dependencies": list(dependencies),
            "terminal_dependencies": [], "gate": gate,
            "static_bindings": [
                *static,
                *([] if profile is None else [_named("task-profile", profile)]),
                _named("task-protocol", protocol),
            ],
            "output_path": output, "receipt_path": receipt, "run_id": run_id,
            "logical_profile_id": logical_id,
            "expected_configuration_template_sha256": fast_configuration_template_sha256(configuration, provenance, baseline_task_id),
            "reference_profile_id": reference_profile_id,
            "reference_profile_sha256": reference_profile_sha256,
            "baseline_task_id": baseline_task_id,
            "baseline_artifact": None if baseline_artifact is None else dict(baseline_artifact),
            "ranking_evidence": ranking, "suite_id": suite["suite_id"],
            "expected_case_count": suite["case_count"], "manifest": dict(suite["manifest"]),
            "profile": profile, "measurement_protocol": protocol,
            "compiler_artifact": dict(compiler_artifact),
            "execution_environment_sha256": source["execution_environment_sha256"],
        }
        tasks.append(task)
        args = [
            "{python}", "-I", "-m", "tools.benchmark", "run", suite["manifest"]["path"],
            "--workspace-root", ".", "--output", output, "--state-dir", f"{state_root}/{safe_slug(task_id)}",
            "--run-id", run_id, "--repo-commit", bootstrap["evaluation_revision"]["commit"],
            "--repo-dirty", "false", "--pipeline-profile-id", logical_id,
            *(
                ["--pipeline-profile-sha256", reference_profile_sha256]
                if profile is None
                else ["--pipeline-profile-file", profile["path"]]
            ),
            *(
                []
                if reference_profile_id is not None
                else [
                    "--candidate-registry", source["artifacts"]["candidate_registry"]["path"],
                    "--candidate-pass-registry", source["artifacts"]["executable_pass_registry"]["path"],
                ]
            ),
            "--compiler-artifact", compiler_artifact["path"],
            "--execution-environment-sha256", source["execution_environment_sha256"],
            "--measurement-protocol", protocol["path"], "--compile-repetitions", "5",
            "--repetitions", "1", "--jobs", "4", "--seed", "20260809",
            "--timeout-policy", "initial" if baseline_task_id is None else "baseline_derived",
            "--metric-profile", "rv64gc-qemu-v1",
            *( ["--metric-extension", "cache-hotblock-v1"] if mode == "cache_hotblock" else [] ),
            "--environment-label", "proxy", "--evidence-level", "qemu_proxy",
            "--output-contract", "lf_return_trailer", *blueprint["argv_tail"],
            "--candidate-fast-plan", plan_relative, "--candidate-fast-status", "{status_path}",
            "--candidate-fast-index", "{index_path}", "--candidate-fast-task-id", task_id,
            "--candidate-fast-receipt", receipt,
            *( ["--baseline-timeout-run", "{baseline_run_path}"] if baseline_task_id is not None else [] ),
        ]
        launch_rows.append(
            {
                "task_id": task_id,
                "selector": selector,
                "baseline_task_id": baseline_task_id,
                "baseline_artifact": None if baseline_artifact is None else dict(baseline_artifact),
                "configuration_template_sha256": task["expected_configuration_template_sha256"],
                "argv": args,
            }
        )

    add_pseudo("audit.bootstrap", "audit", "bootstrap", [], gate="always")
    for candidate in candidates:
        add_run(
            f"run.B2.{candidate}", kind="run", stage="B2", selected=[candidate], mode="standard_proxy",
            dependencies=["audit.bootstrap"], gate="dependencies_succeeded", baseline_task_id="run.B2.full",
            baseline_artifact=b2_baseline, ranking=False, selector="accela-standard",
        )
    add_pseudo("study.B2", "study", "B2", [], terminal=[f"run.B2.{item}" for item in candidates], gate="dependencies_terminal")
    add_pseudo("audit.B2", "audit", "B2", ["study.B2"])
    add_run("run.B3.full", kind="run", stage="B3", selected=[], mode="standard_proxy", dependencies=["audit.B2"], gate="dependencies_succeeded", baseline_task_id=None, baseline_artifact=None, ranking=False, selector="accela-standard")
    add_run("run.B3.gcc", kind="run", stage="B3", selected=[], mode="standard_proxy", dependencies=["audit.B2"], gate="dependencies_succeeded", baseline_task_id=None, baseline_artifact=None, ranking=False, selector="reference-gcc", reference_profile_id="gcc-13.3-o2")
    add_run("run.B3.clang", kind="run", stage="B3", selected=[], mode="standard_proxy", dependencies=["audit.B2"], gate="dependencies_succeeded", baseline_task_id=None, baseline_artifact=None, ranking=False, selector="reference-clang", reference_profile_id="clang-18-o3")
    for candidate in candidates:
        add_run(f"run.B3.{candidate}", kind="run", stage="B3", selected=[candidate], mode="standard_proxy", dependencies=["run.B3.full"], gate="dependencies_succeeded", baseline_task_id="run.B3.full", baseline_artifact=None, ranking=True, selector="accela-standard")
    add_pseudo("study.B3", "study", "B3", ["run.B3.full"], terminal=["run.B3.gcc", "run.B3.clang", *[f"run.B3.{item}" for item in candidates]], gate="dependencies_terminal")
    add_pseudo("audit.B3", "audit", "B3", ["study.B3"])

    for role in ("B4", "B5", "B6"):
        add_run(f"run.{role}.full", kind="run", stage=role, selected=[], mode="standard_proxy", dependencies=["audit.B3"], gate="dependencies_succeeded", baseline_task_id=None, baseline_artifact=None, ranking=False, selector="accela-standard")
        for candidate in candidates:
            add_run(f"run.{role}.{candidate}", kind="run", stage=role, selected=[candidate], mode="standard_proxy", dependencies=["audit.B3", f"run.{role}.full"], gate="candidate_eligible", baseline_task_id=f"run.{role}.full", baseline_artifact=None, ranking=True, selector="accela-standard")
        add_pseudo(f"study.{role}", "study", role, [f"run.{role}.full"], terminal=[f"run.{role}.{item}" for item in candidates], gate="candidate_eligible")

    for left, right in combinations(candidates, 2):
        selected = tuple(sorted((left, right)))
        add_run(f"diagnostic.pair.{'+'.join(selected)}", kind="diagnostic", stage="diagnostic", selected=selected, mode="standard_proxy", dependencies=["study.B3", "run.B3.full"], gate="diagnostic_top3", baseline_task_id="run.B3.full", baseline_artifact=None, ranking=False, selector="accela-standard")
    add_run("diagnostic.cache.full", kind="diagnostic", stage="diagnostic", selected=[], mode="cache_hotblock", dependencies=["study.B3"], gate="dependencies_succeeded", baseline_task_id=None, baseline_artifact=None, ranking=False, selector="accela-cache")
    for candidate in candidates:
        add_run(f"diagnostic.cache.{candidate}", kind="diagnostic", stage="diagnostic", selected=[candidate], mode="cache_hotblock", dependencies=["study.B3", "diagnostic.cache.full"], gate="diagnostic_top3", baseline_task_id="diagnostic.cache.full", baseline_artifact=None, ranking=False, selector="accela-cache")
    diagnostic_ids = [task["task_id"] for task in tasks if task["kind"] == "diagnostic"]
    add_pseudo("study.diagnostic", "study", "diagnostic", [], terminal=diagnostic_ids, gate="dependencies_terminal")
    add_pseudo("audit.final", "audit", "final", ["study.B3", "study.diagnostic"], terminal=["study.B4", "study.B5", "study.B6"])
    add_pseudo("final", "final", "final", ["audit.final", "study.diagnostic"], terminal=["study.B4", "study.B5", "study.B6"], gate="final_ready")

    plan = build_fast_campaign_plan(
        plan_id=plan_id, bootstrap_path=bootstrap_physical, workspace_root=root,
        tasks=tasks, candidate_ids=candidates, created_at=created_at,
    )
    templates = {
        "schema_version": _TEMPLATE_VERSION,
        "campaign_id": plan["campaign_id"],
        "plan_sha256": sha256_json(plan),
        "max_parallel_runs": 4,
        "jobs_per_run": 4,
        "tasks": launch_rows,
        "template_commitment_sha256": "0" * 64,
    }
    templates["template_commitment_sha256"] = sha256_json(
        {key: value for key, value in templates.items() if key != "template_commitment_sha256"}
    )
    _publish_immutable(plan_physical, plan, label="fast campaign plan")
    _publish_immutable(launch_physical, templates, label="fast launch templates")
    return plan, templates


def materialize_fast_launch_spec(
    *, workspace_root: Path, template_path: Path, head_path: Path, output_path: Path,
) -> list[dict[str, Any]]:
    """Materialize only the current runnable wave, resolving baselines from head/index."""

    root = _root(workspace_root)
    template_physical, _ = _inside(root, template_path, label="fast launch templates", exists=True)
    head_physical, _ = _inside(root, head_path, label="fast current head", exists=True)
    output_physical, _ = _inside(root, output_path, label="fast launch output", exists=False)
    templates = read_json(template_physical)
    head = load_and_validate(head_physical)
    if templates.get("schema_version") != _TEMPLATE_VERSION:
        raise ValidationError("fast launch template version differs")
    if set(templates) != {
        "schema_version", "campaign_id", "plan_sha256", "max_parallel_runs",
        "jobs_per_run", "tasks", "template_commitment_sha256"
    } or templates["template_commitment_sha256"] != sha256_json(
        {key: value for key, value in templates.items() if key != "template_commitment_sha256"}
    ):
        raise ValidationError("fast launch template commitment differs")
    plan_path = _verify_artifact(root, head["status"], label="fast head status")
    status = load_and_validate(plan_path)
    plan_physical = _verify_artifact(root, status["plan"], label="fast status plan")
    index_physical = _verify_artifact(root, head["index"], label="fast head index")
    plan = load_and_validate(plan_physical)
    index = load_and_validate(index_physical)
    if templates["campaign_id"] != head["campaign_id"] or templates["plan_sha256"] != sha256_json(plan):
        raise ValidationError("fast launch templates bind another campaign plan")
    measured_tasks = [task for task in plan["tasks"] if task["kind"] in {"run", "diagnostic"}]
    task_by_id = {task["task_id"]: task for task in plan["tasks"]}
    receipt_by_task = {row["task_id"]: row for row in index["receipts"]}
    template_by_id = {row["task_id"]: row for row in templates["tasks"]}
    if (
        len(template_by_id) != len(templates["tasks"])
        or set(template_by_id) != {task["task_id"] for task in measured_tasks}
        or any(
            template_by_id[task["task_id"]]["configuration_template_sha256"]
            != task["expected_configuration_template_sha256"]
            for task in measured_tasks
        )
    ):
        raise ValidationError("fast launch templates differ from the exact measured plan tasks")
    runnable = [
        task_id for task_id in status["ready_tasks"]
        if task_by_id[task_id]["kind"] in {"run", "diagnostic"}
    ]
    if len(runnable) > 4:
        raise ValidationError("fast materialized wave exceeds four runs")
    result: list[dict[str, Any]] = []
    for task_id in runnable:
        row = template_by_id.get(task_id)
        if row is None:
            raise ValidationError("fast ready run lacks its launch template")
        baseline_path: str | None = None
        baseline_task_id = row["baseline_task_id"]
        if baseline_task_id is not None:
            if row["baseline_artifact"] is not None:
                _verify_artifact(root, row["baseline_artifact"], label=f"fast {task_id} imported baseline")
                baseline_path = row["baseline_artifact"]["path"]
            else:
                receipt_ref = receipt_by_task.get(baseline_task_id)
                if receipt_ref is None:
                    raise ValidationError(f"fast {task_id} baseline receipt is absent from current index")
                receipt_path = _verify_artifact(root, receipt_ref["receipt"], label=f"fast {task_id} baseline receipt")
                receipt = load_and_validate(receipt_path)
                if receipt["terminal"]["state"] != "completed":
                    raise ValidationError(f"fast {task_id} baseline is not completed")
                _verify_artifact(root, receipt["run_artifact"], label=f"fast {task_id} baseline run")
                baseline_path = receipt["run_artifact"]["path"]
        replacements = {
            "{python}": sys.executable,
            "{status_path}": head["status"]["path"],
            "{index_path}": head["index"]["path"],
            "{baseline_run_path}": baseline_path,
        }
        argv: list[str] = []
        for item in row["argv"]:
            value = replacements.get(item, item)
            if value is None:
                raise ValidationError(f"fast {task_id} has an unresolved launch placeholder")
            argv.append(value)
        result.append({"task_id": task_id, "argv": argv})
    _publish_immutable(output_physical, result, label="fast materialized launch spec")
    return result
