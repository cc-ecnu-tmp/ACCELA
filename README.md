# ACCELA

ACCELA is a SysY compiler for RISC-V RV64GC. The competition-facing command is
kept deliberately small and stable:

```sh
./gradlew classes --no-daemon
java -cp build/classes/java/main Compiler testcase.sy -S -o testcase.s -O1
```

JDK 21 is required. The default `Compiler` entry always uses the complete
competition pipeline and does not emit benchmark diagnostics.

## Compiler pipeline

The frontend performs lexing, parsing, semantic analysis, and AST-to-SSA IR
lowering. Registered IR and machine passes then optimize and lower the program
to RV64GC assembly using the LP64D ABI and `medany` code model. Required
lowering, SSA construction/destruction, register allocation, and assembly
emission cannot be disabled.

`BenchmarkCompiler` is the development-only entry for deterministic pass
profiles and JSONL optimization remarks. It does not change the formal compiler
interface. See `docs/optimization/README.md` for the evaluation workflow.

## Correctness and proxy performance tools

The clean-room SysY corpus under `benchmarks/` contains mature workload shapes,
structural variants, and paired semantic oracles. Regenerate and verify it with:

```sh
python benchmarks/generate.py --check
python -m unittest discover -s benchmarks/tests -v
```

The candidate evaluator runs compile/link/QEMU correctness and instruction-count
comparisons with CLI-controlled parallelism:

```sh
python3 scripts/evaluate_candidates.py --max-runs 6 --jobs 4
```

QEMU dynamic instructions, memory accesses, and the documented L1D model are
proxy evidence only. They must not be reported as BOOM v3 or official contest
cycle results. See `tools/qemu/README.md` for the bare-metal runtime and plugin
measurement boundary.
