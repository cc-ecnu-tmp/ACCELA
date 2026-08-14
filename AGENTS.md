# ACCELA Engineering Contract

## Product

- ACCELA compiles SysY to RISC-V RV64GC with the LP64D ABI and `medany` code model.
- The judge interface remains `compiler INPUT -S -o OUTPUT -O1`.
- Evaluation controls belong to `BenchmarkCompiler`; they must not change the judge pipeline.

## Correctness

- Fail immediately on compile, link, runtime, timeout, malformed metric, or output mismatch.
- A candidate with any failed case is not rankable.
- Benchmark logic must never dispatch on benchmark names, paths, hashes, or known outputs.
- Preserve input bytes exactly and compare the complete program output, including the uint8 return trailer.
- QEMU instruction counts are proxy evidence, not BOOM hardware results.

## Evaluation

- `scripts/evaluate_candidates.py` is the only candidate-evaluation orchestrator.
- It runs B2--B6 directly with CLI-controlled run and case concurrency, resumes passed cases, and writes plain JSON plus Markdown under an ignored output directory.
- Do not add campaign plans, schemas, hashes, ledgers, receipts, status heads, audit documents, or compatibility paths.
- Keep only the five active manifests and seven pipeline profiles used by the script.
- Run formal evaluation from a Linux-native filesystem. Never use DrvFS/9p for measurements.

## Change discipline

- Use a dedicated branch for optimization experiments.
- Reuse the compiler, linker, QEMU runtime, and plugins.
- Run affected Java tests plus the evaluator self-test before handoff.
