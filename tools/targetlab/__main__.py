from __future__ import annotations

import argparse
import json
import shutil
import shlex
import sys
import tempfile
from pathlib import Path

from .config import validate_config
from .profilec import _expected_metrics, canonical_json, embed, load_json, parse_json, profile_from_raw
from .runner import build, run_baremetal, run_linux
from .schema import ValidationError, validate_profile


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.targetlab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure = subparsers.add_parser("configure")
    configure.add_argument("--backend", choices=("linux", "baremetal"), required=True)
    configure.add_argument("--cc", required=True)
    configure.add_argument("--objcopy", required=True)
    configure.add_argument("--nm", required=True)
    configure.add_argument("--output", type=Path, required=True)
    configure.add_argument("--build-dir", default="build/targetlab")
    configure.add_argument("--clock-hz", type=int, required=True)
    configure.add_argument("--minimum-cycles", type=int, default=1_000_000)
    configure.add_argument("--timeout-seconds", type=int, default=3600)
    configure.add_argument("--execute", default="")
    configure.add_argument("--gdb", default="")
    configure.add_argument("--gdb-remote", default="localhost:3333")
    configure.add_argument("--openocd", default="openocd")
    configure.add_argument("--openocd-config", default="")
    configure.add_argument("--openocd-mode", choices=("managed", "external"), default="managed")
    configure.add_argument("--startup", default="")
    configure.add_argument("--linker", default="")
    for name in ("build", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("config", type=Path)
        command.add_argument("--root", type=Path, default=Path.cwd())
        if name == "run":
            command.add_argument("--output", type=Path, required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("input", type=Path)
    collect.add_argument("output", type=Path)
    profile = subparsers.add_parser("profile")
    profile.add_argument("raw", type=Path)
    profile.add_argument("template", type=Path)
    profile.add_argument("output", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("profile", type=Path)
    embed_parser = subparsers.add_parser("embed")
    embed_parser.add_argument("profile", type=Path)
    embed_parser.add_argument("output", type=Path)
    verify = subparsers.add_parser("verify-embedded")
    verify.add_argument("profile", type=Path)
    verify.add_argument("output", type=Path)
    report = subparsers.add_parser("report")
    report.add_argument("profile", type=Path)
    report.add_argument("output", type=Path)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("config", type=Path)
    doctor.add_argument("--root", type=Path, default=Path.cwd())
    selftest = subparsers.add_parser("selftest")
    selftest.add_argument("--template", type=Path,
        default=Path("config/target/boomv3-development.json"))

    args = parser.parse_args(argv)
    if args.command == "configure":
        _configure(args)
    elif args.command == "build":
        build(validate_config(load_json(args.config)), args.root)
    elif args.command == "run":
        config = validate_config(load_json(args.config))
        if config["backend"] == "linux":
            run_linux(config, args.root, args.output)
        else:
            run_baremetal(config, args.root, args.output)
    elif args.command == "collect":
        _collect(args.input, args.output)
    elif args.command == "profile":
        result = profile_from_raw(load_json(args.template), load_json(args.raw))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(canonical_json(result), encoding="utf-8", newline="\n")
    elif args.command == "validate":
        validate_profile(load_json(args.profile))
    elif args.command == "embed":
        embed(args.profile, args.output)
    elif args.command == "verify-embedded":
        embed(args.profile, args.output, verify=True)
    elif args.command == "report":
        _report(validate_profile(load_json(args.profile)), args.output)
    elif args.command == "doctor":
        _doctor(validate_config(load_json(args.config)), args.root)
    elif args.command == "selftest":
        _selftest(args.template)
    return 0


def _configure(args):
    if args.backend == "linux" and not args.execute:
        raise ValidationError("Linux backend requires --execute")
    if args.backend == "baremetal" and (not args.gdb or not args.startup or not args.linker
            or not args.openocd_config):
        raise ValidationError("baremetal backend requires GDB, OpenOCD config, startup, and linker")
    document = {"backend": args.backend, "cc": args.cc, "objcopy": args.objcopy, "nm": args.nm,
        "build_dir": args.build_dir, "clock_hz": args.clock_hz,
        "minimum_cycles": args.minimum_cycles, "timeout_seconds": args.timeout_seconds}
    if args.backend == "linux":
        document["execute"] = args.execute
    else:
        document.update({"gdb": args.gdb, "gdb_remote": args.gdb_remote,
            "startup": args.startup, "linker": args.linker, "openocd": args.openocd,
            "openocd_config": args.openocd_config, "openocd_mode": args.openocd_mode})
    validate_config(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical_json(document), encoding="utf-8", newline="\n")


def _collect(source, output):
    samples = []
    environment = None
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = parse_json(line, f"target JSON line {line_number}")
        if item.get("kind") == "environment":
            if environment is not None or set(item) != {"kind", "backend", "rdcycle", "rdinstret", "timer"}:
                raise ValidationError(f"target JSON environment at line {line_number} is invalid or duplicated")
            environment = {key: item[key] for key in ("backend", "rdcycle", "rdinstret", "timer")}
        elif item.get("kind") == "sample":
            if set(item) != {"kind", "metric", "category", "source", "values"}:
                raise ValidationError(f"target JSON sample at line {line_number} has invalid keys")
            samples.append({key: item[key] for key in ("metric", "category", "source", "values")})
        else:
            raise ValidationError(f"target JSON line {line_number} has unknown kind")
    if environment is None:
        raise ValidationError("target JSON is missing its environment record")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json({"schema_version": 1, "environment": environment,
        "samples": samples}),
        encoding="utf-8", newline="\n")


def _report(profile, output):
    environment = profile["measurement_environment"]
    lines = [f"# TargetProfile {profile['profile']['id']}", "",
        f"- Calibrated: `{str(profile['profile']['calibrated']).lower()}`",
        f"- Target: `{profile['target']['isa']}` / `{profile['target']['abi']}` / `{profile['target']['code_model']}`",
        f"- Core: {profile['target']['clock_hz']} Hz, issue width {profile['target']['issue_width']}",
        f"- Measurement backend: `{environment['backend']}`",
        f"- Timer: `{environment['timer']}`; rdcycle=`{_capability(environment['rdcycle'])}`; rdinstret=`{_capability(environment['rdinstret'])}`",
        f"- SIMD enabled: `{str(profile['simd']['enabled']).lower()}`", "", "## Operation measurements", "",
        "| Class | Latency | MAD | Throughput | MAD |", "|---|---:|---:|---:|---:|"]
    for name, operation in profile["operations"].items():
        lines.append(f"| {name} | {operation['latency']['median']:.4f} | {operation['latency']['mad']:.4f} | "
            f"{operation['throughput']['median']:.4f} | {operation['throughput']['mad']:.4f} |")
    lines.extend(("", "## Diagnostic curves", "", "| Metric | Point | Median | MAD | Samples |",
        "|---|---:|---:|---:|---:|"))
    diagnostics = profile["diagnostics"]
    for name in ("load_use", "pointer_chase"):
        item = diagnostics[name]
        lines.append(f"| {name} | - | {item['median']:.4f} | {item['mad']:.4f} | {item['sample_count']} |")
    for group in ("working_set", "stride", "frontend", "register_pressure"):
        for point, item in diagnostics[group].items():
            lines.append(f"| {group} | {point} | {item['median']:.4f} | {item['mad']:.4f} | {item['sample_count']} |")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _capability(value):
    return "unmeasured" if value is None else str(value).lower()


def _doctor(config, root):
    required = ["make", config["cc"], config["objcopy"], config["nm"]]
    if config["backend"] == "baremetal":
        required.extend((config["gdb"], config["openocd"]))
        for name in ("startup", "linker", "openocd_config"):
            path = (root / config[name]).resolve()
            if not path.is_file():
                raise ValidationError(f"configured {name} file does not exist: {config[name]}")
    else:
        execute = shlex.split(config["execute"])
        if not execute:
            raise ValidationError("Linux execute command is empty")
        required.append(execute[0])
    missing = [command for command in required if not _executable_exists(command, root)]
    if missing:
        raise ValidationError(f"required executables are unavailable: {', '.join(missing)}")
    print(f"TargetLab doctor: {config['backend']} configuration is ready")


def _executable_exists(command, root):
    if "/" in command or "\\" in command:
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.is_file()
    return shutil.which(command) is not None


def _selftest(template_path):
    template = validate_profile(load_json(template_path))
    with tempfile.TemporaryDirectory(prefix="targetlab-selftest-") as temporary:
        root = Path(temporary)
        raw_jsonl = root / "raw.jsonl"
        collected = root / "collected.json"
        profile_path = root / "profile.json"
        generated = root / "GeneratedTargetProfile.java"
        report = root / "report.md"
        records = [{"kind": "environment", "backend": "linux", "rdcycle": True,
            "rdinstret": True, "timer": "rdcycle"}]
        records.extend({"kind": "sample", "metric": metric, "category": "selftest",
            "source": "rdcycle_x1000", "values": [1000] * 9}
            for metric in sorted(_expected_metrics(template)))
        raw_jsonl.write_text("".join(json.dumps(record, separators=(",", ":")) + "\n"
            for record in records), encoding="utf-8", newline="\n")
        _collect(raw_jsonl, collected)
        profile = profile_from_raw(template, load_json(collected))
        profile_path.write_text(canonical_json(profile), encoding="utf-8", newline="\n")
        embed(profile_path, generated)
        embed(profile_path, generated, verify=True)
        _report(profile, report)
        if not report.read_text(encoding="utf-8").startswith("# TargetProfile"):
            raise ValidationError("selftest report generation failed")
    print("TargetLab selftest: collect/profile/validate/embed/report passed")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, RuntimeError, OSError, KeyError) as exception:
        print(f"targetlab: error: {exception}", file=sys.stderr)
        raise SystemExit(2) from exception
