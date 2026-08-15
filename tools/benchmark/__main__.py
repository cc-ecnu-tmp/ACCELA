import argparse
import json
import sys
from pathlib import Path

from .report import (BenchmarkError, analyze, import_linux_tsv, import_qemu_tsv, load_json, render,
                     validate_manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-manifest")
    validate.add_argument("manifest", type=Path)
    report = commands.add_parser("report")
    report.add_argument("results", type=Path)
    report.add_argument("output", type=Path)
    qemu = commands.add_parser("import-qemu")
    qemu.add_argument("input", type=Path)
    qemu.add_argument("output", type=Path)
    qemu.add_argument("--comparison", required=True,
        choices=("r1_full", "r2_r1", "r2_llvm"))
    qemu.add_argument("--target", required=True)
    qemu.add_argument("--abi", required=True)
    qemu.add_argument("--runtime", required=True)
    linux = commands.add_parser("import-linux")
    linux.add_argument("input", type=Path)
    linux.add_argument("output", type=Path)
    linux.add_argument("--comparison", required=True,
        choices=("r1_full", "r2_r1", "r2_llvm"))
    linux.add_argument("--target", required=True)
    linux.add_argument("--abi", required=True)
    linux.add_argument("--runtime", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        manifest = validate_manifest(args.manifest)
        print(f"benchmark manifest: {len(manifest['cases'])} cases validated")
    elif args.command == "report":
        document = load_json(args.results)
        output = render(document, analyze(document))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8", newline="\n")
    elif args.command == "import-qemu":
        document = import_qemu_tsv(args.input, args.comparison,
            args.target, args.abi, args.runtime)
        analyze(document)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
    else:
        document = import_linux_tsv(args.input, args.comparison,
            args.target, args.abi, args.runtime)
        analyze(document)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BenchmarkError, OSError) as exception:
        print(f"benchmark: error: {exception}", file=sys.stderr)
        raise SystemExit(2) from exception
