# ACCELA optimization log

This is the durable record for each research, implementation, correctness, and performance loop. Generated per-case JSON lives in `bench-results/`; this file keeps the decisions and headline numbers that must survive cleanup.

## 2026-07-10 — Baseline infrastructure audit

### Scope and hypothesis

- Treat `testsuite/functional` (100 cases) plus `testsuite/h_functional` (40 cases) as the official 140-case corpus.
- Establish correctness and static RISC-V code-quality baselines before changing compiler behavior.
- Use assembled machine-instruction count and `.text` bytes as the first portable performance proxies. Dynamic executed cycles still need a Linux RISC-V execution environment and must not be inferred from static counts.

### Findings

- Repository baseline: ACCELA commit `d6c4e87` (`liveness analysis`). The working tree was initially clean.
- The old `testsuite/hidden_functional/test.sh` runs only 40 cases from a different directory and requires `riscv64-linux-gnu-gcc`, `qemu-riscv64`, GNU `timeout`, and `dos2unix`; those tools are not a usable macOS one-command baseline.
- The system default JDK 8 cannot compile the source's records, pattern matching, and switch expressions. The installed JDK 26 is too new for Gradle 8.14. Homebrew JDK 21 was installed and the Gradle unit tests pass with it.
- The comparison compiler was cloned at commit `fcc3633` and builds successfully from its 95 C++ sources with Clang C++20 `-O3`.
- ACCELA, the comparison compiler, and LLVM 22.1.8 `-O3` all emit RISC-V assembly for the initial smoke case, and LLVM MC accepts all three outputs.

### Infrastructure added

- `scripts/build.sh`: selects a supported JDK, runs a clean Gradle build plus unit tests, and creates `build/src/main`.
- `scripts/benchmark.py`: selects the exact 140 cases, runs native optimized-IR correctness in parallel, produces assembly with all three compilers, verifies it with LLVM MC, counts disassembled RISC-V machine instructions and `.text` bytes, and saves detailed JSON.
- `scripts/sysy-runtime.h`: minimal declarations/macros that let LLVM compile SysY sources for RISC-V without a target libc sysroot.

### Validation results

- Gradle unit tests: pass.
- Optimized-IR native correctness: **140/140 pass**.
- RISC-V assembly validation: **140/140 assemble** for each of ACCELA, the comparison compiler, and LLVM `-O3`.
- Static RISC-V machine instructions across the corpus:
  - ACCELA: **2,644,288** instructions, 9,006,760 `.text` bytes.
  - Comparison compiler: **29,373** instructions, 87,412 `.text` bytes.
  - LLVM 22.1.8 `-O3`: **30,877** instructions, 92,916 `.text` bytes.
- The comparison/ACCELA instruction ratio is **0.011108**; LLVM/ACCELA is **0.011677**. Equivalently, ACCELA emits about 90.0x and 85.6x as many static instructions, respectively. These are static code-size proxies, not dynamic speed ratios.

### Hotspot decomposition

- `h_functional/30_many_dimensions.sy` alone contributes 2,103,498 ACCELA instructions versus 348 from the comparison compiler.
- `functional/86_long_code2.sy` contributes 330,228 versus 3.
- Those two cases account for about 92.0% of ACCELA's aggregate instruction count. The backend expands every `MEMZERO` into one store per four bytes, so large local arrays create millions of straight-line stores.
- Outside those outliers, the current `AllSpillRegisterAllocator` still forces every virtual register to memory. The generated code repeatedly reloads operands and stores results, compounding the remaining gap.
- Detailed per-case evidence: `bench-results/baseline-20260711-001120.json` (also copied to `bench-results/latest.json`).

### Keep / defer decision

- Keep the baseline infrastructure: it completed the full corpus, retained per-case evidence, and did not abort after individual stages.
- Defer dynamic-cycle claims until a reproducible RISC-V Linux runner exists. Static counts are explicitly labeled as proxies.
- Prioritize structured frontend cleanup as requested, then address `MEMZERO` lowering and all-spill register allocation; the baseline now makes the payoff of both backend changes directly measurable.

## 2026-07-10 — Typed numeric values at the parser boundary

### Research and hypothesis

- The official [SysY 2022 language definition](https://gitlab.eduxiji.net/nscscc/compiler2022/-/blob/master/SysY2022%E8%AF%AD%E8%A8%80%E5%AE%9A%E4%B9%89-V1.pdf) defines `Number -> IntConst | FloatConst`, 32-bit signed `int`, 32-bit `float`, C-compatible literal syntax, and ignored suffixes (pages 1, 3-4).
- ACCELA already had structural enums for token kinds, operators, and source types. The remaining violation was `Node.s` also carrying numeric spellings, which Sema, the interpreter, and AST2IR each reparsed differently.
- Hypothesis: parse the spelling once into a typed 32-bit value, then pass only that value through later stages. Runtime assembly should be identical; the intended gain is a simpler and consistent frontend representation.

### Implementation

- Added `LiteralValue` with explicit integer/float kind, C/SysY radix handling, hexadecimal floats, suffix handling, conversion, zero checks, and debug-only text creation.
- `Node` now has a dedicated typed literal field. Parser token text is converted immediately; Sema constant folding creates typed values directly.
- Interpreter and AST2IR consume numeric accessors. Their duplicate string parsers and the unused `Ty.fromString` path were deleted.
- Builtin return types are registered with `Ty` objects rather than string type names.

### Validation and decision

- Unit tests: pass, including decimal/octal/hex integers, 32-bit wrap, suffixes, decimal/hex floats, and runtime number kind.
- Focused optimized-IR and interpreter tests for integer return, hexadecimal/octal values, and float-heavy programs: pass.
- Full optimized-IR correctness: **140/140 pass**.
- Full ACCELA RISC-V assembly validation: **140/140 assemble**.
- Performance proxy: **2,644,288 instructions and 9,006,760 `.text` bytes**, exactly unchanged from baseline as predicted.
- **Keep**: neutral runtime performance, but it satisfies the structural frontend requirement and removes three inconsistent internal parsing implementations. Next loop targets the measured `MEMZERO` explosion.
