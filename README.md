# ACCELA

ACCELA is a Java SysY compiler with an LLVM-like middle-end and a RISC-V backend.

## Build

The project uses language features newer than Java 8. Gradle 8.14 currently works with JDK 17-24; the reproducible build script prefers Homebrew JDK 21 and runs the unit tests:

```bash
./scripts/build.sh
```

On macOS, install the pinned JDK with `brew install openjdk@21` if it is missing. The script creates the competition-compatible launcher at `build/src/main`.

## Correctness and performance baseline

The official corpus is the 100 cases in `testsuite/functional` plus the 40 cases in `testsuite/h_functional`. Run the full baseline with:

```bash
python3 scripts/benchmark.py baseline
```

This command runs all 140 cases through ACCELA's optimized IR and executes them natively for correctness. It also emits RISC-V assembly with ACCELA, the comparison compiler in `thirdparty/sysy-competition`, and LLVM `-O3`; all outputs must assemble, after which real machine instructions and `.text` bytes are counted from the RISC-V objects. The comparison repository is cloned automatically when absent.

Useful narrower commands:

```bash
python3 scripts/benchmark.py correctness
python3 scripts/benchmark.py benchmark
python3 scripts/benchmark.py baseline --filter 94_nested_loops --skip-build
```

Machine-readable results are written under `bench-results/`, with the newest run copied to `bench-results/latest.json`. Static instruction count measures generated code quality but is not a substitute for dynamic cycle measurements; the tracked status and limitations are recorded in `docs/optimization-log.md`.

## Frontend

### Lexer

### Parser

#### AST Design

### Sema

### IRBuilder
