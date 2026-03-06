#!/usr/bin/env python3
"""
Usage: python3 tests/test_ir.py [test_pattern]
"""
import subprocess, sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Gradle: classes in build/classes/java/main, main class accela.Main
ACCELA = ['java', '-cp', str(ROOT / 'build' / 'classes' / 'java' / 'main'), 'accela.Main', '--ir']
SYLIB_OBJ = str(ROOT / 'build' / 'sylib.o')
SYLIB_SRC = str(ROOT / 'testsuite' / 'libsysy' / 'sylib.c')


def ensure_sylib():
    if not os.path.exists(SYLIB_OBJ):
        subprocess.run(['cc', '-c', SYLIB_SRC, '-o', SYLIB_OBJ], check=True)


def run_test(sy_file):
    out_file = sy_file.with_suffix('.out')
    in_file = sy_file.with_suffix('.in')
    if not out_file.exists():
        return None, "Missing .out"

    try:
        ir_proc = subprocess.run(
            ACCELA + [str(sy_file)],
            capture_output=True, text=True, timeout=10
        )
        if ir_proc.returncode != 0:
            return False, f"IR gen failed:\n{ir_proc.stderr.strip()}"
        ir_text = ir_proc.stdout
    except subprocess.TimeoutExpired:
        return False, "IR gen timeout"

    ll_path = '/tmp/accela_test.ll'
    bin_path = '/tmp/accela_test_bin'
    with open(ll_path, 'w') as f:
        f.write(ir_text)

    try:
        clang_proc = subprocess.run(
            ['clang', ll_path, SYLIB_OBJ, '-o', bin_path, '-lm'],
            capture_output=True, text=True, timeout=30
        )
        if clang_proc.returncode != 0:
            return False, f"Clang failed:\n{clang_proc.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "Clang timeout"

    input_data = ""
    if in_file.exists():
        with open(in_file) as f:
            input_data = f.read()

    try:
        run_proc = subprocess.run(
            [bin_path],
            input=input_data,
            capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return False, "Runtime timeout"

    program_output = run_proc.stdout
    exit_code = run_proc.returncode % 256
    actual_lines = [l for l in program_output.splitlines()]
    actual_lines.append(str(exit_code))
    actual = ' '.join(' '.join(actual_lines).split())

    with open(out_file) as f:
        expected = ' '.join(f.read().split())

    if actual == expected:
        return True, ""
    else:
        return False, f"Output mismatch.\nExpected: {expected}\nActual:   {actual}"


def main():
    ensure_sylib()

    test_dirs = [
        ROOT / 'testsuite' / 'functional',
    ]
    pattern = sys.argv[1] if len(sys.argv) > 1 else None

    sy_files = sorted(f for d in test_dirs if d.exists() for f in d.glob('*.sy'))
    if pattern:
        sy_files = [f for f in sy_files if pattern in f.name]

    print(f"Running {len(sy_files)} IR tests...\n")

    passed = 0
    failed = []

    for i, sy_file in enumerate(sy_files):
        success, msg = run_test(sy_file)
        name = sy_file.name
        if success is True:
            passed += 1
            print(f"[{i+1:3d}/{len(sy_files)}] {name} ... PASS")
        elif success is False:
            failed.append((name, msg))
            print(f"[{i+1:3d}/{len(sy_files)}] {name} ... FAIL")
        else:
            print(f"[{i+1:3d}/{len(sy_files)}] {name} ... SKIP ({msg})")

    print(f"\n{'='*40}")
    print(f"Results: {passed}/{len(sy_files)} passed")
    print(f"{'='*40}")

    if failed:
        print(f"\n{len(failed)} failures:")
        for name, msg in failed:
            print(f"\n--- {name} ---")
            print(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
