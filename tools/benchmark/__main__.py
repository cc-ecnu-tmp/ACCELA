import argparse
import sys
from pathlib import Path

from .report import BenchmarkError, analyze, load_json, render, validate_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-manifest")
    validate.add_argument("manifest", type=Path)
    report = commands.add_parser("report")
    report.add_argument("results", type=Path)
    report.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-manifest":
        manifest = validate_manifest(args.manifest)
        print(f"benchmark manifest: {len(manifest['cases'])} cases validated")
    else:
        document = load_json(args.results)
        output = render(document, analyze(document))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BenchmarkError, OSError) as exception:
        print(f"benchmark: error: {exception}", file=sys.stderr)
        raise SystemExit(2) from exception
