from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .profilec import canonical_json, embed, load_json, profile_from_raw
from .runner import build, run_baremetal, run_linux
from .schema import ValidationError, validate_profile


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.targetlab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure = subparsers.add_parser("configure")
    configure.add_argument("--backend", choices=("linux", "baremetal"), required=True)
    configure.add_argument("--cc", required=True)
    configure.add_argument("--objcopy", required=True)
    configure.add_argument("--output", type=Path, required=True)
    configure.add_argument("--build-dir", default="build/targetlab")
    configure.add_argument("--execute", default="")
    configure.add_argument("--gdb", default="")
    configure.add_argument("--gdb-remote", default="localhost:3333")
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

    args = parser.parse_args(argv)
    if args.command == "configure":
        _configure(args)
    elif args.command == "build":
        build(load_json(args.config), args.root)
    elif args.command == "run":
        config = load_json(args.config)
        if config["backend"] == "linux":
            run_linux(config, args.root, args.output)
        else:
            run_baremetal(config, args.root, args.output)
    elif args.command == "collect":
        _collect(args.input, args.output)
    elif args.command == "profile":
        result = profile_from_raw(load_json(args.template), load_json(args.raw))
        args.output.write_text(canonical_json(result), encoding="utf-8", newline="\n")
    elif args.command == "validate":
        validate_profile(load_json(args.profile))
    elif args.command == "embed":
        embed(args.profile, args.output)
    elif args.command == "verify-embedded":
        embed(args.profile, args.output, verify=True)
    elif args.command == "report":
        _report(validate_profile(load_json(args.profile)), args.output)
    return 0


def _configure(args):
    if args.backend == "linux" and not args.execute:
        raise ValidationError("Linux backend requires --execute")
    if args.backend == "baremetal" and (not args.gdb or not args.startup or not args.linker):
        raise ValidationError("baremetal backend requires --gdb, --startup, and --linker")
    document = {"backend": args.backend, "cc": args.cc, "objcopy": args.objcopy,
        "build_dir": args.build_dir}
    if args.backend == "linux":
        document["execute"] = args.execute
    else:
        document.update({"gdb": args.gdb, "gdb_remote": args.gdb_remote,
            "startup": args.startup, "linker": args.linker})
    args.output.write_text(canonical_json(document), encoding="utf-8", newline="\n")


def _collect(source, output):
    samples = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exception:
            raise ValidationError(f"invalid target JSON at line {line_number}: {exception}") from exception
        if set(item) != {"metric", "category", "source", "values"}:
            raise ValidationError(f"target JSON line {line_number} has invalid keys")
        samples.append(item)
    output.write_text(canonical_json({"schema_version": 1, "samples": samples}),
        encoding="utf-8", newline="\n")


def _report(profile, output):
    lines = [f"# TargetProfile {profile['profile']['id']}", "",
        f"- Calibrated: `{str(profile['profile']['calibrated']).lower()}`",
        f"- Target: `{profile['target']['isa']}` / `{profile['target']['abi']}` / `{profile['target']['code_model']}`",
        f"- Core: {profile['target']['clock_hz']} Hz, issue width {profile['target']['issue_width']}",
        f"- SIMD enabled: `{str(profile['simd']['enabled']).lower()}`", "", "## Operation measurements", "",
        "| Class | Latency | MAD | Throughput | MAD |", "|---|---:|---:|---:|---:|"]
    for name, operation in profile["operations"].items():
        lines.append(f"| {name} | {operation['latency']['median']:.4f} | {operation['latency']['mad']:.4f} | "
            f"{operation['throughput']['median']:.4f} | {operation['throughput']['mad']:.4f} |")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, RuntimeError, OSError, KeyError) as exception:
        print(f"targetlab: error: {exception}", file=sys.stderr)
        raise SystemExit(2) from exception
