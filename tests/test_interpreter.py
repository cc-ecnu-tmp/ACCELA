import subprocess
import sys
import os
from pathlib import Path

def run_test(sy_file):
    out_file = sy_file.with_suffix('.out')
    in_file = sy_file.with_suffix('.in')

    if not out_file.exists():
        return None, "Missing .out file"

    input_data = ""
    if in_file.exists():
        with open(in_file, 'r') as f:
            input_data = f.read()

    try:
        root_dir = Path(__file__).resolve().parent.parent
        accela_cmd = ['java', '-cp', str(root_dir / 'build' / 'classes' / 'java' / 'main'), 'accela.Main']
        proc = subprocess.run(
            accela_cmd + ['--interpret', str(sy_file)],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=10
        )

        full_output = proc.stdout.strip()
        lines = [line.strip() for line in full_output.splitlines() if line.strip()]

        if lines:
            exit_val = lines[-1]
            program_output = lines[:-1]
        else:
            exit_val = ""
            program_output = []

        final_output_lines = program_output + ([exit_val] if exit_val != "" else [])
        actual_result = " ".join(" ".join(final_output_lines).split())

        with open(out_file, 'r') as f:
            expected_result = " ".join(f.read().split())

        if actual_result == expected_result:
            return True, ""
        else:
            return False, f"Mismatch.\nExpected:\n{expected_result}\nActual:\n{actual_result}"

    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, f"Error: {str(e)}"

def main():
    test_dirs = [
        Path('testsuite/functional'),
        Path('testsuite/hidden_functional'),
    ]
    pattern = sys.argv[1] if len(sys.argv) > 1 else None

    sy_files = sorted(f for d in test_dirs if d.exists() for f in d.glob('*.sy'))
    if pattern:
        sy_files = [f for f in sy_files if pattern in f.name or pattern == f.parent.name]

    print(f"Running {len(sy_files)} functional tests with AST Interpreter...\n")

    passed = 0
    failed = []

    for i, sy_file in enumerate(sy_files):
        success, msg = run_test(sy_file)
        if success is True:
            passed += 1
            print(f"[{i+1:3d}/{len(sy_files)}] {sy_file.parent.name}/{sy_file.name} ... PASSED")
        elif success is False:
            failed.append((f"{sy_file.parent.name}/{sy_file.name}", msg))
            print(f"[{i+1:3d}/{len(sy_files)}] {sy_file.parent.name}/{sy_file.name} ... FAILED")
        else:
            pass

    print("\n" + "="*30)
    print(f"Final Results: {passed}/{len(sy_files)} Passed")
    print("="*30)

    if failed:
        print("\nFailures:")
        for name, msg in failed:
            print(f"--- {name} ---")
            print(msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
