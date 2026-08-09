from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ablation import build_ablation_remark
from .audit import build_cross_suite_audit
from .campaign import (
    build_campaign_plan,
    campaign_task,
    finalize_campaign_plan,
    next_campaign_tasks,
    update_campaign_status,
)
from .adapters import StageSpec
from .errors import BenchmarkError, ConfigurationError
from .execution import MeasurementSpec, RunOptions, RunProvenance, ToolVersion, run_benchmark
from .inventory import inventory_cleanroom_manifest, inventory_suite, subset_manifest
from .metrics import cache_hotblock_metrics_v1, rv64gc_qemu_v1
from .oracle import build_oracle_plan, prepare_oracle_leg_manifest
from .profiles import generate_ablation_profiles
from .protocol import capture_measurement_protocol, verify_measurement_protocol
from .report import build_report
from .schema import load_and_validate, load_and_validate_jsonl
from .util import (
    atomic_write_json,
    parse_command_json,
    parse_environment,
    read_json,
    sanitize_text,
    sha256_artifact,
    sha256_json,
)


def _path(value: str) -> Path:
    return Path(value)


def _verify_git_provenance(
    workspace_root: Path,
    *,
    declared_commit: str,
    declared_dirty: bool,
) -> None:
    """Verify that execution provenance describes the repository being run.

    Formal records must not be able to claim an arbitrary clean commit.  Keep
    diagnostics path-free because the workspace location is intentionally not
    part of normalized benchmark evidence.
    """

    root = workspace_root.resolve(strict=True)

    def git(
        *arguments: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
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
            raise ConfigurationError("unable to verify Git provenance") from exc
        if result.returncode not in allowed_returncodes:
            raise ConfigurationError("workspace is not a readable Git worktree")
        return result

    actual_commit = (
        git("rev-parse", "--verify", "HEAD")
        .stdout.decode("ascii", errors="strict")
        .strip()
        .lower()
    )
    if actual_commit != declared_commit.lower():
        raise ConfigurationError("declared repository commit differs from workspace HEAD")

    # A single porcelain status scan can exceed a short subprocess deadline on
    # large WSL worktrees mounted from NTFS.  Use Git's plumbing commands so
    # staged, unstaged, and untracked state remain independently observable and
    # fail-fast without weakening the clean-worktree contract.
    staged_dirty = git(
        "diff-index",
        "--cached",
        "--quiet",
        "HEAD",
        "--",
        allowed_returncodes=(0, 1),
    ).returncode == 1
    unstaged_dirty = git(
        "diff-files", "--quiet", "--", allowed_returncodes=(0, 1)
    ).returncode == 1
    untracked_dirty = bool(
        git(
            "ls-files",
            "--others",
            "--exclude-standard",
            "--directory",
            "--no-empty-directory",
            "-z",
        ).stdout
    )
    actual_dirty = staged_dirty or unstaged_dirty or untracked_dirty
    if actual_dirty != declared_dirty:
        state = "dirty" if actual_dirty else "clean"
        raise ConfigurationError(f"declared repository state differs from {state} workspace")


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _worker_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= 4:
        raise argparse.ArgumentTypeError("must be between 1 and 4")
    return parsed


def _parse_assignments(values: Sequence[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise ConfigurationError(f"{label} must use NAME=VALUE")
        if key in result:
            raise ConfigurationError(f"duplicate {label}: {key}")
        result[key] = item
    return result


def _tool_versions(actual_values: Sequence[str], expected_values: Sequence[str]) -> tuple[ToolVersion, ...]:
    actual = _parse_assignments(actual_values, "tool version")
    expected = _parse_assignments(expected_values, "official version")
    unknown = sorted(expected.keys() - actual.keys())
    if unknown:
        raise ConfigurationError(f"official version lacks an actual tool observation: {', '.join(unknown)}")
    return tuple(
        ToolVersion(tool, version, expected.get(tool))
        for tool, version in sorted(actual.items())
    )


def _measurement_specs(values: Sequence[str]) -> tuple[MeasurementSpec, ...]:
    result: list[MeasurementSpec] = []
    allowed = {"metric_id", "source", "unit", "pattern"}
    for raw in values:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("measurement-json must be a JSON object") from exc
        if not isinstance(value, dict) or set(value) - allowed:
            raise ConfigurationError(
                "measurement-json accepts only metric_id, source, unit, and optional pattern"
            )
        required = {"metric_id", "source", "unit"}
        if not required.issubset(value) or not all(isinstance(value[key], str) for key in required):
            raise ConfigurationError("measurement-json requires string metric_id, source, and unit")
        pattern = value.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            raise ConfigurationError("measurement-json pattern must be a string or omitted")
        result.append(MeasurementSpec(value["metric_id"], value["source"], value["unit"], pattern))
    return tuple(result)


def _stage(
    *,
    command_json: str | None,
    label: str,
    required: bool,
    kind: str,
    adapter: str,
    environment_values: Sequence[str],
) -> StageSpec | None:
    command = parse_command_json(command_json, label=label, required=required)
    if command is None:
        if environment_values:
            raise ConfigurationError(f"{label} environment was supplied without a command")
        return None
    return StageSpec(kind, adapter, command, parse_environment(environment_values))


def _run_options(args: argparse.Namespace, *, oracle: bool) -> RunOptions:
    manifest_path = args.manifest.resolve(strict=True)
    suite_root = (args.suite_root or manifest_path.parent).resolve(strict=True)
    workspace_root = args.workspace_root.resolve(strict=True)
    output_path = args.output.resolve()
    state_root = (args.state_dir or (output_path.parent / ".benchmark-state")).resolve()
    compiler = _stage(
        command_json=args.compiler_command_json,
        label="compiler-command-json",
        required=True,
        kind=args.compiler_kind,
        adapter=args.compiler_adapter,
        environment_values=args.compiler_env,
    )
    assert compiler is not None
    if oracle and compiler.kind != "benchmark-compiler":
        raise ConfigurationError("oracle source legs must use the ACCELA BenchmarkCompiler entry point")
    linker = _stage(
        command_json=args.link_command_json,
        label="link-command-json",
        required=False,
        kind="external",
        adapter=args.link_adapter,
        environment_values=args.link_env,
    )
    analyzer = _stage(
        command_json=args.analyzer_command_json,
        label="analyzer-command-json",
        required=False,
        kind="analyzer",
        adapter=args.analyzer_adapter,
        environment_values=args.analyzer_env,
    )
    runner = _stage(
        command_json=args.runner_command_json,
        label="runner-command-json",
        required=True,
        kind=args.runner_kind,
        adapter=args.runner_adapter,
        environment_values=args.runner_env,
    )
    assert runner is not None
    if args.metric_profile is not None:
        if any(value is not None for value in (args.primary_metric_id, args.metric_source, args.metric_pattern, args.metric_unit)) or args.measurement_json:
            raise ConfigurationError("--metric-profile cannot be combined with individual metric options")
        preset = rv64gc_qemu_v1()
        primary_metric_id = str(preset["primary_metric_id"])
        metric_source = str(preset["metric_source"])
        metric_pattern = str(preset["metric_pattern"])
        metric_unit = str(preset["metric_unit"])
        metric_file = args.metric_file or str(preset["metric_file"])
        analysis_file = args.analysis_file or str(preset["analysis_file"])
        extension = (
            cache_hotblock_metrics_v1()
            if args.metric_extension == "cache-hotblock-v1"
            else []
        )
        additional_metrics = tuple(
            MeasurementSpec(
                str(item["metric_id"]), str(item["source"]), str(item["unit"]),
                None if item["pattern"] is None else str(item["pattern"]),
            )
            for item in [*preset["additional"], *extension]
        )
    else:
        if args.metric_extension is not None:
            raise ConfigurationError("--metric-extension requires --metric-profile")
        primary_metric_id = args.primary_metric_id or "wall_time_ns"
        metric_source = args.metric_source or "wall_time"
        metric_pattern = args.metric_pattern
        metric_unit = args.metric_unit or ("ns" if metric_source == "wall_time" else None)
        metric_file = args.metric_file
        analysis_file = args.analysis_file
        additional_metrics = _measurement_specs(args.measurement_json)
    if metric_unit is None:
        raise ConfigurationError("--metric-unit is required for stdout/stderr/file primary metrics")
    pipeline_profile_sha256 = (
        args.pipeline_profile_sha256
        if args.pipeline_profile_sha256 is not None
        else sha256_artifact(args.pipeline_profile_file)
    )
    measurement_protocol = (
        load_and_validate(args.measurement_protocol.resolve(strict=True))
        if args.measurement_protocol is not None
        else None
    )
    if measurement_protocol is not None and measurement_protocol["schema_version"] != "measurement-protocol.v1":
        raise ConfigurationError("--measurement-protocol must be measurement-protocol.v1")
    provenance = RunProvenance(
        repo_commit=args.repo_commit,
        repo_dirty=args.repo_dirty == "true",
        tracked_diff_sha256=args.tracked_diff_sha256,
        pipeline_profile_id=args.pipeline_profile_id,
        pipeline_profile_sha256=pipeline_profile_sha256,
        compiler_artifact_sha256=sha256_artifact(args.compiler_artifact),
        measurement_protocol_id=(
            None if measurement_protocol is None else measurement_protocol["protocol_id"]
        ),
        measurement_protocol_sha256=(
            None if measurement_protocol is None else sha256_json(measurement_protocol)
        ),
    )
    return RunOptions(
        manifest_path=manifest_path,
        suite_root=suite_root,
        workspace_root=workspace_root,
        output_path=output_path,
        state_root=state_root,
        compiler=compiler,
        linker=linker,
        runner=runner,
        provenance=provenance,
        pipeline_profile_path=args.pipeline_profile_file,
        measurement_protocol_path=args.measurement_protocol,
        measurement_protocol_assets=tuple(
            (key, Path(value))
            for key, value in _parse_assignments(args.measurement_asset, "measurement asset").items()
        ),
        analyzer=analyzer,
        compile_timeout_seconds=args.compile_timeout,
        compile_repetitions=args.compile_repetitions,
        reuse_compile_cache=args.reuse_compile_cache,
        link_timeout_seconds=args.link_timeout,
        analyze_timeout_seconds=args.analyze_timeout,
        run_timeout_seconds=args.run_timeout,
        timeout_policy=args.timeout_policy,
        baseline_timeout_path=args.baseline_timeout_run,
        timeout_minimum_seconds=args.timeout_minimum,
        timeout_multiplier=args.timeout_multiplier,
        timeout_cap_seconds=args.timeout_cap,
        repetitions=args.repetitions,
        max_workers=args.jobs,
        keep_going=args.keep_going,
        retry_failures=args.retry_failures,
        seed=args.seed,
        artifact_suffix=args.artifact_suffix,
        binary_suffix=args.binary_suffix,
        primary_metric_id=primary_metric_id,
        metric_source=metric_source,
        metric_pattern=metric_pattern,
        metric_unit=metric_unit,
        metric_profile_id=args.metric_profile,
        metric_file=metric_file,
        analysis_file=analysis_file,
        output_contract=args.output_contract,
        result_file=args.result_file,
        remarks_file=args.remarks_file,
        additional_metrics=additional_metrics,
        wsl_executable=args.wsl_executable,
        wsl_distribution=args.wsl_distribution,
        run_id=args.run_id,
        environment_label=args.environment_label,
        evidence_level=args.evidence_level,
        tool_versions=_tool_versions(args.tool_version, args.official_version),
    )


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("manifest", type=_path)
    parser.add_argument("--suite-root", type=_path)
    parser.add_argument("--workspace-root", type=_path, default=Path.cwd())
    parser.add_argument("--output", type=_path, required=True)
    parser.add_argument("--state-dir", type=_path)
    parser.add_argument("--run-id")
    parser.add_argument("--repo-commit", required=True, help="full Git object id")
    parser.add_argument("--repo-dirty", required=True, choices=("true", "false"))
    parser.add_argument("--tracked-diff-sha256")
    parser.add_argument("--pipeline-profile-id", required=True)
    profile = parser.add_mutually_exclusive_group(required=True)
    profile.add_argument("--pipeline-profile-sha256")
    profile.add_argument("--pipeline-profile-file", type=_path)
    parser.add_argument("--compiler-artifact", type=_path, required=True, help="compiler binary or classes directory to hash")
    parser.add_argument(
        "--measurement-protocol", type=_path,
        help="measurement-protocol.v1 snapshot; required for qemu_proxy evidence",
    )
    parser.add_argument(
        "--measurement-asset", action="append", default=[], metavar="LOGICAL_KEY=PATH",
        help="physical source/plugin/QEMU/runner artifact verified against the protocol snapshot",
    )
    parser.add_argument("--compiler-command-json", required=True)
    parser.add_argument("--compiler-kind", choices=("benchmark-compiler", "external"), default="benchmark-compiler")
    parser.add_argument("--compiler-adapter", choices=("host", "wsl"), default="host")
    parser.add_argument("--compiler-env", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--link-command-json")
    parser.add_argument("--link-adapter", choices=("host", "wsl"), default="host")
    parser.add_argument("--link-env", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--analyzer-command-json")
    parser.add_argument("--analyzer-adapter", choices=("host", "wsl"), default="host")
    parser.add_argument("--analyzer-env", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--analysis-file", help="relative path under each isolated case directory, exposed as {analysis_file}")
    parser.add_argument("--remarks-file", help="relative path under each isolated case directory, exposed as {remarks_file}")
    parser.add_argument("--runner-command-json", required=True)
    parser.add_argument("--runner-kind", choices=("external", "qemu", "boom"), default="qemu")
    parser.add_argument("--runner-adapter", choices=("host", "wsl"), default="host")
    parser.add_argument("--runner-env", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--compile-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--compile-repetitions", type=int, default=5)
    parser.add_argument(
        "--reuse-compile-cache",
        action="store_true",
        help="exploratory mode only; formal ablation rejects cached compile observations",
    )
    parser.add_argument("--link-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--analyze-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--run-timeout", type=_positive_float, default=1800.0)
    parser.add_argument(
        "--timeout-policy", choices=("fixed", "initial", "baseline_derived"), default="initial",
        help="initial uses 1800s; baseline_derived uses min(1800,max(120,3x baseline case))",
    )
    parser.add_argument("--baseline-timeout-run", type=_path)
    parser.add_argument("--timeout-minimum", type=_positive_float, default=120.0)
    parser.add_argument("--timeout-multiplier", type=_positive_float, default=3.0)
    parser.add_argument("--timeout-cap", type=_positive_float, default=1800.0)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--jobs", type=_worker_count, default=1)
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--seed", type=int, default=20260809)
    parser.add_argument("--artifact-suffix", default=".s")
    parser.add_argument("--binary-suffix", default=".elf")
    parser.add_argument("--metric-profile", choices=("rv64gc-qemu-v1",))
    parser.add_argument("--metric-extension", choices=("cache-hotblock-v1",))
    parser.add_argument("--primary-metric-id")
    parser.add_argument("--metric-source", choices=("wall_time", "stdout", "stderr", "file"))
    parser.add_argument("--metric-pattern")
    parser.add_argument("--metric-unit")
    parser.add_argument(
        "--metric-file",
        help="relative path under each isolated case directory, exposed as {metric_file}",
    )
    parser.add_argument(
        "--output-contract",
        choices=("lf_return_trailer", "process_exit", "result_file", "raw_stdout"),
        default="lf_return_trailer",
        help="compare program stdout and uint8 main return independently",
    )
    parser.add_argument("--result-file", help="relative case path exposed as {result_file}")
    parser.add_argument(
        "--measurement-json",
        action="append",
        default=[],
        help="additional metric JSON: metric_id/source/unit and optional pattern",
    )
    parser.add_argument("--wsl-executable", default="wsl.exe")
    parser.add_argument("--wsl-distribution")
    parser.add_argument(
        "--environment-label",
        choices=("official", "local_reference", "proxy"),
        default="local_reference",
    )
    parser.add_argument(
        "--evidence-level",
        choices=("compile_only", "qemu_correctness", "qemu_proxy", "boom_hardware"),
        default="qemu_proxy",
    )
    parser.add_argument("--tool-version", action="append", default=[], metavar="TOOL=ACTUAL")
    parser.add_argument("--official-version", action="append", default=[], metavar="TOOL=EXPECTED")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accela-benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    protocol = subparsers.add_parser(
        "protocol", help="capture or verify physical RV64GC QEMU measurement artifacts"
    )
    protocol_commands = protocol.add_subparsers(dest="protocol_command", required=True)
    for name, help_text in (
        ("capture", "hash sources/plugins/QEMU/runner into measurement-protocol.v1"),
        ("verify", "fail fast if any physical protocol artifact or QEMU config drifted"),
    ):
        command = protocol_commands.add_parser(name, help=help_text)
        if name == "verify":
            command.add_argument("snapshot", type=_path)
        else:
            command.add_argument("--protocol-id", required=True)
            command.add_argument(
                "--measurement-mode",
                choices=("standard_proxy", "cache_hotblock"),
                default="standard_proxy",
            )
            command.add_argument("--machine", required=True)
            command.add_argument("--cpu-model", required=True)
            command.add_argument("--memory", required=True)
            command.add_argument("--output", type=_path, required=True)
        command.add_argument(
            "--asset", action="append", required=True, metavar="LOGICAL_KEY=PATH",
            help="all required source/.so/QEMU/runner physical artifacts",
        )
        command.add_argument("--runner-command-json", required=True)
        command.add_argument("--runner-env", action="append", default=[], metavar="KEY=VALUE")
        command.add_argument("--runner-adapter", choices=("host", "wsl"), default="host")
        command.add_argument("--wsl-executable", default="wsl.exe")
        command.add_argument("--wsl-distribution")

    inventory = subparsers.add_parser("inventory", help="inventory a benchmark suite into benchmark-manifest.v1")
    inventory.add_argument("suite_root", nargs="?", type=_path)
    inventory_source = inventory.add_mutually_exclusive_group()
    inventory_source.add_argument("--cleanroom-manifest", type=_path)
    inventory_source.add_argument("--source-manifest", type=_path, help="derive an exact case-id subset from benchmark-manifest.v1")
    inventory.add_argument("--suite-id", required=True)
    inventory.add_argument("--target", required=True)
    inventory.add_argument("--data-role", required=True, choices=("B1", "B2", "B3", "B4", "B5", "B6", "oracle"))
    inventory.add_argument("--origin-source", required=True, help="logical source snapshot id, never a filesystem path")
    inventory.add_argument("--origin-snapshot-sha256")
    inventory.add_argument("--license-expression", help="SPDX expression or NOASSERTION")
    inventory.add_argument(
        "--validity-status",
        choices=("included", "included_with_exclusions", "excluded"),
        default="included",
    )
    inventory.add_argument(
        "--validity-reason",
        choices=("verified", "packaging_defect", "unsupported", "manual_exclusion", "unknown"),
        default="verified",
    )
    inventory.add_argument("--captured-at", help="explicit RFC3339 timestamp; omitted manifests remain byte-deterministic")
    inventory.add_argument("--source-suffix", default=".sy")
    inventory.add_argument("--ignore-orphans", action="store_true")
    inventory.add_argument("--tier", action="append", default=[], help="clean-room dataset tier filter")
    inventory.add_argument("--family", action="append", default=[], help="clean-room family/id filter")
    inventory.add_argument("--dataset-role", action="append", default=[], help="clean-room correctness/performance filter")
    inventory.add_argument(
        "--oracle-leg", action="append", default=[], choices=("baseline", "optimized"),
        help="clean-room oracle source leg; default imports both",
    )
    inventory.add_argument("--case-id", action="append", default=[], help="case id for --source-manifest subset")
    inventory.add_argument("--case-id-file", type=_path, help="UTF-8 file with one case id per nonblank line")
    inventory.add_argument("--require-one-per-family", action="store_true")
    inventory.add_argument("--output", type=_path, required=True)

    validate = subparsers.add_parser("validate", help="validate schemas or execute the correctness gate")
    validate_commands = validate.add_subparsers(dest="validate_command", required=True)
    validate_schema = validate_commands.add_parser("schema", help="strictly validate benchmark JSON/JSONL documents")
    validate_schema.add_argument("documents", nargs="+", type=_path)
    validate_schema.add_argument("--suite-root", type=_path)
    validate_schema.add_argument("--verify-files", action="store_true")
    validate_suite = validate_commands.add_parser(
        "suite", help="compile, link, execute, and byte-check all cases without performance ranking"
    )
    _add_run_arguments(validate_suite)
    validate_suite.set_defaults(
        primary_metric_id="wall_time_ns",
        metric_source="wall_time",
        metric_pattern=None,
        metric_unit="ns",
        evidence_level="qemu_correctness",
        compile_repetitions=5,
    )

    audit = subparsers.add_parser("audit", help="hash-map two manifests without exposing paths")
    audit.add_argument("left", type=_path)
    audit.add_argument("right", type=_path)
    audit.add_argument("--left-root", type=_path)
    audit.add_argument("--right-root", type=_path)
    audit.add_argument("--output", type=_path, required=True)

    run = subparsers.add_parser("run", help="compile, link, execute, and verify a benchmark suite")
    _add_run_arguments(run)

    oracle = subparsers.add_parser("oracle", help="plan or execute clean-room oracle measurements")
    oracle_commands = oracle.add_subparsers(dest="oracle_command", required=True)
    oracle_plan = oracle_commands.add_parser(
        "plan", help="expand an oracle manifest into baseline/optimized paired run identities"
    )
    oracle_plan.add_argument("manifest", type=_path)
    oracle_plan.add_argument("--suite-root", type=_path)
    oracle_plan.add_argument("--pipeline-profile-id", required=True)
    oracle_plan.add_argument("--pipeline-profile-sha256", required=True)
    oracle_plan.add_argument("--output", type=_path, required=True)
    oracle_run = oracle_commands.add_parser(
        "run", help="execute one trusted oracle pipeline leg from a paired plan"
    )
    oracle_run.add_argument("--plan", type=_path, required=True)
    oracle_run.add_argument("--leg", choices=("baseline", "optimized"), required=True)
    _add_run_arguments(oracle_run)
    oracle_run.set_defaults(compiler_kind="benchmark-compiler", runner_kind="qemu")

    ablate = subparsers.add_parser("ablate", help="generate ablation profiles or analyze paired runs")
    ablate_commands = ablate.add_subparsers(dest="ablate_command", required=True)
    ablate_profiles = ablate_commands.add_parser(
        "profiles", help="generate FULL/mandatory/family/Top5-pair canonical profiles and schedule matrix"
    )
    ablate_profiles.add_argument("--registry", type=_path, required=True)
    ablate_profiles.add_argument("--pair", action="append", default=[], metavar="FAMILY_A+FAMILY_B")
    ablate_profiles.add_argument(
        "--top-family", action="append", default=[], metavar="FAMILY",
        help="up to five ranked families; all nC2 interaction profiles are generated",
    )
    ablate_profiles.add_argument("--output-dir", type=_path, required=True)
    ablate_analyze = ablate_commands.add_parser(
        "analyze", help="build an ablation-study.v1 from completed paired run records"
    )
    ablate_analyze.add_argument("--matrix", type=_path, required=True)
    ablate_analyze.add_argument("baseline", type=_path)
    ablate_analyze.add_argument("--variant", action="append", required=True, metavar="ID=RUN_JSON")
    ablate_analyze.add_argument("--interaction", action="append", default=[], metavar="LEFT+RIGHT=RUN_JSON")
    ablate_analyze.add_argument("--study-id", required=True)
    ablate_analyze.add_argument("--title", required=True)
    ablate_analyze.add_argument("--bootstrap-samples", type=int, default=10_000, choices=(10_000,))
    ablate_analyze.add_argument("--seed", type=int, default=20260809, choices=(20260809,))
    ablate_analyze.add_argument("--output", type=_path, required=True)
    campaign_plan = ablate_commands.add_parser(
        "campaign-plan", help="build the fixed 12h/24h/24h/12h multi-suite campaign plan"
    )
    campaign_plan.add_argument("--matrix", type=_path, required=True)
    campaign_plan.add_argument("--oracle-plan", type=_path, required=True)
    campaign_plan.add_argument("--measurement-protocol", type=_path, required=True)
    campaign_plan.add_argument("--cache-hotblock-protocol", type=_path, required=True)
    campaign_plan.add_argument("--reference-toolchain", type=_path, required=True)
    campaign_plan.add_argument("--workspace-root", type=_path, default=Path.cwd())
    campaign_plan.add_argument(
        "--suite", action="append", required=True, metavar="B1|B2|B3|B4|B5|B6|oracle=MANIFEST",
        help="exactly one normalized manifest for every evidence role",
    )
    campaign_plan.add_argument("--campaign-id", required=True)
    campaign_plan.add_argument("--jobs", type=_worker_count, default=4)
    campaign_plan.add_argument("--output", type=_path, required=True)
    campaign_finalize = ablate_commands.add_parser(
        "campaign-finalize",
        help="bind measured B3 Top5 status to an extended matrix and schedule all ten pairs",
    )
    campaign_finalize.add_argument("--plan", type=_path, required=True)
    campaign_finalize.add_argument("--status", type=_path, required=True)
    campaign_finalize.add_argument("--matrix", type=_path, required=True)
    campaign_finalize.add_argument("--output", type=_path, required=True)
    campaign_status = ablate_commands.add_parser(
        "campaign-status", help="recompute resumable campaign state from completed run records"
    )
    campaign_status.add_argument("--plan", type=_path, required=True)
    campaign_status.add_argument("--run", action="append", default=[], metavar="TASK_ID=RUN_JSON")
    campaign_status.add_argument(
        "--study", action="append", default=[], metavar="singleton_b2|promotion_b3=ABLATION_STUDY_JSON",
        help="normalized study evidence used to derive Top8 promotion and final Top5",
    )
    campaign_status.add_argument("--previous-status", type=_path)
    campaign_status.add_argument(
        "--started-at", help="RFC3339 campaign start; required for the first status record",
    )
    campaign_status.add_argument("--as-of", help="RFC3339 observation time; defaults to current UTC")
    campaign_status.add_argument("--output", type=_path, required=True)
    campaign_next = ablate_commands.add_parser(
        "campaign-next", help="emit the next budget/dependency-safe tasks for the existing run executor"
    )
    campaign_next.add_argument("--plan", type=_path, required=True)
    campaign_next.add_argument("--status", type=_path, required=True)
    campaign_task_query = ablate_commands.add_parser(
        "campaign-task", help="query one immutable task or task field from a campaign plan"
    )
    campaign_task_query.add_argument("--plan", type=_path, required=True)
    campaign_task_query.add_argument("--task-id", required=True)
    campaign_task_query.add_argument("--field")

    report = subparsers.add_parser("report", help="emit JSON, CSV, Markdown, and SVG benchmark reports")
    report.add_argument("run", type=_path)
    report.add_argument("--baseline", type=_path)
    report.add_argument(
        "--baseline-mode", choices=("pipeline_ablation", "cross_toolchain"),
        default="pipeline_ablation",
        help="explicit attribution boundary for --baseline",
    )
    report.add_argument(
        "--comparison", action="append", default=[], metavar="LABEL=RUN_JSON",
        help="additional GCC/Clang-style cross-toolchain reference run",
    )
    report.add_argument("--oracle-plan", type=_path, help="paired-source oracle-plan.v1 for --baseline/run")
    report.add_argument(
        "--candidate-evidence", type=_path,
        help="candidate-evidence.v1 for the independent P0/P1/P2/Blocked implementation ranking",
    )
    report.add_argument(
        "--candidate-plan", action="append", default=[], type=_path,
        help="additional oracle-plan.v1 referenced by candidate evidence",
    )
    report.add_argument(
        "--candidate-run", action="append", default=[], type=_path,
        help="additional run-record.v1 referenced by candidate evidence",
    )
    report.add_argument(
        "--remark",
        action="append",
        default=[],
        metavar="CASE_ID=JSONL",
        help="case-bound optimization-remark.v1 JSONL; content hash must match the run record",
    )
    report.add_argument(
        "--ablation",
        action="append",
        default=[],
        type=_path,
        help="aggregate ablation-study.v1 JSON",
    )
    report.add_argument(
        "--hotblock-run",
        action="append",
        default=[],
        metavar="LABEL=RUN_JSON",
        help="cache-hotblock run-record.v1 used only for traceable hotspot diagnostics",
    )
    report.add_argument("--bootstrap-samples", type=int, default=10_000)
    report.add_argument("--seed", type=int, default=20260809)
    report.add_argument("--output-dir", type=_path, required=True)
    return parser


def _parse_interactions(values: Sequence[str]) -> dict[tuple[str, str], Path]:
    result: dict[tuple[str, str], Path] = {}
    for key, path in _parse_assignments(values, "interaction").items():
        left, separator, right = key.partition("+")
        if not separator or not left or not right or "+" in right or left == right:
            raise ConfigurationError("interaction must use LEFT+RIGHT=RUN_JSON with distinct variants")
        pair = tuple(sorted((left, right)))
        if pair in result:
            raise ConfigurationError(f"duplicate interaction: {left}+{right}")
        result[pair] = Path(path)
    return result


def _parse_top_pairs(values: Sequence[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for value in values:
        left, separator, right = value.partition("+")
        if not separator or not left or not right or "+" in right:
            raise ConfigurationError("Top-pair must use FAMILY_A+FAMILY_B")
        result.append((left, right))
    return result


def _case_ids(arguments: argparse.Namespace) -> list[str]:
    result = list(arguments.case_id)
    if arguments.case_id_file is not None:
        try:
            lines = arguments.case_id_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("cannot read UTF-8 case-id file") from exc
        for line_number, line in enumerate(lines, 1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if any(character.isspace() for character in value):
                raise ConfigurationError(f"case-id file line {line_number} contains whitespace")
            result.append(value)
    if len(result) != len(set(result)):
        raise ConfigurationError("case-id selection contains duplicates")
    return result


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "protocol":
        runner_command = parse_command_json(
            args.runner_command_json, label="runner-command-json", required=True
        )
        assert runner_command is not None
        runner = StageSpec("qemu", args.runner_adapter, runner_command, parse_environment(args.runner_env))
        assets = {
            key: Path(value) for key, value in _parse_assignments(args.asset, "protocol asset").items()
        }
        if args.protocol_command == "capture":
            snapshot = capture_measurement_protocol(
                protocol_id=args.protocol_id,
                assets=assets,
                runner=runner,
                machine=args.machine,
                cpu_model=args.cpu_model,
                memory=args.memory,
                measurement_mode=args.measurement_mode,
                wsl_executable=args.wsl_executable,
                wsl_distribution=args.wsl_distribution,
            )
            atomic_write_json(args.output, snapshot)
            print(json.dumps({"schema_version": snapshot["schema_version"], "sha256": sha256_json(snapshot)}))
            return 0
        snapshot = load_and_validate(args.snapshot)
        verify_measurement_protocol(
            snapshot,
            assets=assets,
            runner=runner,
            wsl_executable=args.wsl_executable,
            wsl_distribution=args.wsl_distribution,
        )
        print(json.dumps({"schema_version": snapshot["schema_version"], "sha256": sha256_json(snapshot), "verified": True}))
        return 0
    if args.command == "inventory":
        if args.cleanroom_manifest is not None:
            if args.suite_root is not None or args.source_manifest is not None:
                raise ConfigurationError("clean-room inventory must not also supply suite_root/source-manifest")
            if args.case_id or args.case_id_file is not None or args.require_one_per_family:
                raise ConfigurationError("case-id subset options require --source-manifest")
            manifest = inventory_cleanroom_manifest(
                args.cleanroom_manifest,
                suite_id=args.suite_id,
                target=args.target,
                data_role=args.data_role,
                origin_source=args.origin_source,
                origin_snapshot_sha256=args.origin_snapshot_sha256,
                license_expression=args.license_expression,
                tiers=args.tier,
                families=args.family,
                dataset_roles=args.dataset_role,
                oracle_legs=args.oracle_leg or ("baseline", "optimized"),
                captured_at=args.captured_at,
            )
        elif args.source_manifest is not None:
            if args.suite_root is None:
                raise ConfigurationError("--source-manifest requires suite_root for file verification")
            if args.tier or args.family or args.dataset_role or args.oracle_leg:
                raise ConfigurationError("clean-room filters require --cleanroom-manifest")
            manifest = subset_manifest(
                args.source_manifest,
                suite_root=args.suite_root,
                suite_id=args.suite_id,
                case_ids=_case_ids(args),
                data_role=args.data_role,
                origin_source=args.origin_source,
                origin_snapshot_sha256=args.origin_snapshot_sha256,
                license_expression=args.license_expression,
                require_one_per_family=args.require_one_per_family,
                captured_at=args.captured_at,
            )
        else:
            if args.suite_root is None:
                raise ConfigurationError("inventory requires suite_root, --cleanroom-manifest, or --source-manifest")
            if args.tier or args.family or args.dataset_role or args.oracle_leg:
                raise ConfigurationError("clean-room filters require --cleanroom-manifest")
            if args.case_id or args.case_id_file is not None or args.require_one_per_family:
                raise ConfigurationError("case-id subset options require --source-manifest")
            if args.origin_snapshot_sha256 is None or args.license_expression is None:
                raise ConfigurationError("filesystem inventory requires origin-snapshot-sha256 and license-expression")
            manifest = inventory_suite(
                args.suite_root,
                suite_id=args.suite_id,
                target=args.target,
                data_role=args.data_role,
                origin_source=args.origin_source,
                origin_snapshot_sha256=args.origin_snapshot_sha256,
                license_expression=args.license_expression,
                validity_status=args.validity_status,
                validity_reason=args.validity_reason,
                captured_at=args.captured_at,
                source_suffix=args.source_suffix,
                ignore_orphans=args.ignore_orphans,
            )
        atomic_write_json(args.output, manifest)
        print(json.dumps({"schema_version": "benchmark-manifest.v1", "cases": len(manifest["cases"]), "orphans": manifest["data_quality"]["orphan_count"]}))
        return 0
    if args.command == "validate" and args.validate_command == "schema":
        versions: dict[str, int] = {}
        for path in args.documents:
            if path.suffix.lower() == ".jsonl":
                events = load_and_validate_jsonl(path)
                versions["optimization-remark.v1"] = versions.get("optimization-remark.v1", 0) + len(events)
                continue
            raw = read_json(path)
            version = raw.get("schema_version") if isinstance(raw, dict) else None
            suite_root = args.suite_root
            if args.verify_files and version == "benchmark-manifest.v1" and suite_root is None:
                suite_root = path.resolve(strict=True).parent
            document = load_and_validate(path, suite_root=suite_root, verify_files=args.verify_files)
            versions[document["schema_version"]] = versions.get(document["schema_version"], 0) + 1
        print(json.dumps({"valid": len(args.documents), "schemas": versions}, sort_keys=True))
        return 0
    if args.command == "validate" and args.validate_command == "suite":
        if args.primary_metric_id != "wall_time_ns" or args.metric_source != "wall_time":
            raise ConfigurationError("validate suite fixes the primary metric to wall_time_ns for correctness evidence")
        if args.evidence_level != "qemu_correctness":
            raise ConfigurationError("validate suite must use qemu_correctness evidence level")
        _verify_git_provenance(
            args.workspace_root,
            declared_commit=args.repo_commit,
            declared_dirty=args.repo_dirty == "true",
        )
        record = run_benchmark(_run_options(args, oracle=False))
        print(json.dumps({
            "schema_version": record["schema_version"],
            "validation": "passed" if record["state"] == "completed" else "failed",
            "run_id": record["run_id"],
            "summary": record["summary"],
        }, sort_keys=True))
        return 0 if record["state"] == "completed" else 1
    if args.command == "audit":
        result = build_cross_suite_audit(
            left_path=args.left,
            right_path=args.right,
            left_root=args.left_root,
            right_root=args.right_root,
        )
        atomic_write_json(args.output, result)
        print(json.dumps({
            "schema_version": result["schema_version"],
            "identical_content_multiset": result["identical_content_multiset"],
            "counts": result["counts"],
        }, sort_keys=True))
        return 0
    if args.command == "run" or (args.command == "oracle" and args.oracle_command == "run"):
        _verify_git_provenance(
            args.workspace_root,
            declared_commit=args.repo_commit,
            declared_dirty=args.repo_dirty == "true",
        )
        if args.command == "oracle":
            manifest_path = args.manifest.resolve(strict=True)
            suite_root = (args.suite_root or manifest_path.parent).resolve(strict=True)
            output_path = args.output.resolve()
            state_root = (args.state_dir or (output_path.parent / ".benchmark-state")).resolve()
            profile_sha256 = (
                args.pipeline_profile_sha256
                if args.pipeline_profile_sha256 is not None
                else sha256_artifact(args.pipeline_profile_file)
            )
            derived = state_root / "oracle" / f"{args.leg}.manifest.json"
            prepare_oracle_leg_manifest(
                plan_path=args.plan,
                manifest_path=manifest_path,
                suite_root=suite_root,
                leg=args.leg,
                run_id=args.run_id,
                pipeline_profile_id=args.pipeline_profile_id,
                pipeline_profile_sha256=profile_sha256,
                output_path=derived,
            )
            args.manifest = derived
            args.suite_root = suite_root
        record = run_benchmark(_run_options(args, oracle=args.command == "oracle"))
        print(json.dumps({"run_id": record["run_id"], "state": record["state"], "summary": record["summary"]}, sort_keys=True))
        return 0 if record["state"] == "completed" else 1
    if args.command == "oracle" and args.oracle_command == "plan":
        suite_root = args.suite_root or args.manifest.resolve(strict=True).parent
        plan = build_oracle_plan(
            manifest_path=args.manifest,
            suite_root=suite_root,
            pipeline_profile_id=args.pipeline_profile_id,
            pipeline_profile_sha256=args.pipeline_profile_sha256,
        )
        atomic_write_json(args.output, plan)
        print(json.dumps({"schema_version": plan["schema_version"], "pairs": len(plan["pairs"])}))
        return 0
    if args.command == "ablate" and args.ablate_command == "profiles":
        matrix = generate_ablation_profiles(
            registry_path=args.registry,
            output_directory=args.output_dir,
            top_pairs=_parse_top_pairs(args.pair),
            top_families=args.top_family,
        )
        print(
            json.dumps(
                {
                    "schema_version": matrix["schema_version"],
                    "profiles": len(matrix["profiles"]),
                    "scheduled": len(matrix["schedule"]),
                }
            )
        )
        return 0
    if args.command == "ablate" and args.ablate_command == "analyze":
        variants = {key: Path(value) for key, value in _parse_assignments(args.variant, "variant").items()}
        remark = build_ablation_remark(
            matrix_path=args.matrix,
            baseline_path=args.baseline,
            variant_paths=variants,
            interaction_paths=_parse_interactions(args.interaction),
            study_id=args.study_id,
            title=args.title,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
        )
        atomic_write_json(args.output, remark)
        print(json.dumps({"schema_version": remark["schema_version"], "variants": len(remark["variants"]), "interactions": len(remark["interactions"])}))
        return 0
    if args.command == "ablate" and args.ablate_command == "campaign-plan":
        suite_paths = {
            key: Path(value) for key, value in _parse_assignments(args.suite, "campaign suite").items()
        }
        plan = build_campaign_plan(
            matrix_path=args.matrix, suite_paths=suite_paths,
            oracle_plan_path=args.oracle_plan,
            measurement_protocol_path=args.measurement_protocol,
            hotblock_measurement_protocol_path=args.cache_hotblock_protocol,
            reference_toolchain_path=args.reference_toolchain,
            workspace_root=args.workspace_root,
            campaign_id=args.campaign_id, max_workers=args.jobs,
        )
        atomic_write_json(args.output, plan)
        print(json.dumps({"schema_version": plan["schema_version"], "tasks": len(plan["tasks"]), "budget_seconds": plan["total_budget_seconds"]}))
        return 0
    if args.command == "ablate" and args.ablate_command == "campaign-status":
        status = update_campaign_status(
            plan_path=args.plan,
            run_paths={key: Path(value) for key, value in _parse_assignments(args.run, "campaign run").items()},
            study_paths={key: Path(value) for key, value in _parse_assignments(args.study, "campaign study").items()},
            previous_status_path=args.previous_status,
            started_at=args.started_at,
            as_of=args.as_of,
        )
        atomic_write_json(args.output, status)
        print(json.dumps({"schema_version": status["schema_version"], "state": status["state"], "remaining_wall_clock_seconds": status["remaining_wall_clock_seconds"]}))
        return 0
    if args.command == "ablate" and args.ablate_command == "campaign-finalize":
        plan = finalize_campaign_plan(
            plan_path=args.plan,
            status_path=args.status,
            matrix_path=args.matrix,
        )
        atomic_write_json(args.output, plan)
        print(json.dumps({
            "schema_version": plan["schema_version"],
            "tasks": len(plan["tasks"]),
            "final_pair_families": plan["final_pair_families"],
        }))
        return 0
    if args.command == "ablate" and args.ablate_command == "campaign-next":
        plan = load_and_validate(args.plan)
        status = load_and_validate(args.status)
        tasks = next_campaign_tasks(plan, status)
        print(json.dumps({"tasks": tasks}, sort_keys=True))
        return 0
    if args.command == "ablate" and args.ablate_command == "campaign-task":
        plan = load_and_validate(args.plan)
        result = campaign_task(plan, task_id=args.task_id, field=args.field)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "report":
        hotblock_run_paths = {
            key: Path(value)
            for key, value in _parse_assignments(
                args.hotblock_run, "cache-hotblock run"
            ).items()
        }
        summary = build_report(
            run_path=args.run,
            output_directory=args.output_dir,
            baseline_path=args.baseline,
            baseline_mode=args.baseline_mode,
            comparison_paths={key: Path(value) for key, value in _parse_assignments(args.comparison, "comparison").items()},
            oracle_plan_path=args.oracle_plan,
            candidate_evidence_path=args.candidate_evidence,
            candidate_plan_paths=args.candidate_plan,
            candidate_run_paths=args.candidate_run,
            remark_paths={key: Path(value) for key, value in _parse_assignments(args.remark, "remark").items()},
            ablation_paths=args.ablation,
            hotblock_run_paths=hotblock_run_paths,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
        )
        artifacts = [
            "cases.csv", "summary.json", "report.md", "speedups.svg", "ablation-waterfall.svg",
            "family-pass-heatmap.svg", "toolchain-gap.svg", "oracle-scaling.svg", "benefit-cost-risk-pareto.svg",
        ]
        if hotblock_run_paths:
            artifacts.append("hotblocks.csv")
        print(json.dumps({"schema_version": summary["schema_version"], "run_id": summary["run_id"], "artifacts": artifacts}))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        return dispatch(parser.parse_args(argv))
    except (BenchmarkError, OSError) as exc:
        print(f"error: {sanitize_text(str(exc), (Path.cwd(),))}", file=sys.stderr)
        return 2
