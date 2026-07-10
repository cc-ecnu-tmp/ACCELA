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

## 2026-07-10 — Thresholded large zero-fill lowering

### Research and hypothesis

- LLVM upstream was cloned locally at `fe80624d` with Transforms, RISC-V, and SelectionDAG sources. `RISCVProcessors.td` sets `MaxStoresPerMemset` to 8; `SelectionDAG.cpp` emits stores up to the target limit and otherwise lowers to a libcall.
- A local LLVM RISC-V probe inlined 64 bytes as eight 8-byte stores and called `memset` for 128 bytes. ACCELA's stack objects are only guaranteed 4-byte alignment, so its equivalent eight-store threshold is 32 bytes.
- The [RISC-V psABI](https://riscv-non-isa.github.io/riscv-elf-psabi-doc/) assigns the first integer arguments to caller-saved `a0-a2`. Large zero fills can therefore use `memset(destination, 0, bytes)`, provided lowering marks the function as calling so the frame saves/restores `ra`.

### Implementation

- Added a 4 KiB zero-initialization microbenchmark; pre-change ACCELA emitted 2,750 instructions versus 15 from both comparison compilers.
- Zero fills of at most 32 bytes remain inline. Larger fills materialize the destination/zero/size in `a0-a2` and call `memset`.
- Machine lowering marks libcall-using functions before frame layout, including functions with no source-level calls.
- Regression tests cover both sides of the threshold and verify `ra` preservation.

### Validation and decision

- Unit tests: pass. Microbenchmark optimized-IR correctness and assembly validation: pass.
- Microbenchmark: **2,750 -> 194 instructions** and **8,704 -> 572 `.text` bytes**.
- Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Full corpus: **2,644,288 -> 507,545 instructions (-80.81%)** and **9,006,760 -> 1,625,882 `.text` bytes (-81.95%)**.
- `h_functional/30_many_dimensions.sy`: **2,103,498 -> 8,397 instructions**. The next largest case, `functional/86_long_code2.sy`, is unchanged at 330,228 and is not dominated by `MEMZERO`.
- **Keep**: decisive code-size reduction, matches upstream lowering policy, and should improve dynamic performance by delegating large fills to libc. Dynamic RISC-V execution still needs the Linux runner before claiming a cycle speedup.

## 2026-07-11 — Basic-block early common-subexpression elimination

### Research and hypothesis

- `functional/86_long_code2.sy` repeats one global-array access 4,000 times. After the old pipeline it still contained 4,000 loads, 4,001 GEPs, and 3,999 adds, producing 330,228 machine instructions.
- LLVM `EarlyCSE.cpp` tracks simple expressions and available loads, assigns memory generations to invalidate values after writes, and forwards the last stored value at an exact pointer. LLVM GVN separately canonicalizes GEPs by address calculation.
- A conservative block-local version is sufficient here: CSE identical expressions, remember exact-pointer loads/stores, and clear all available memory values at every store or call.

### Implementation

- Added block-local expression keys covering opcode, result type, predicates, GEP metadata, and structurally equal integer/float constants.
- Repeated loads are replaced only when the exact SSA pointer is available. A store clears all prior load facts, then exposes its own exact pointer/value; calls clear all load facts.
- EarlyCSE runs before and after SCCP. The first run merges repeated index calculations; SCCP folds them; the second run merges the now-identical GEP and forwards the store; InstSimplify folds the resulting constant add chain.
- Regression tests cover expression CSE, equivalent-GEP store forwarding, and invalidation by stores and calls.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- `functional/86_long_code2.sy`: **330,228 -> 39 instructions**; optimized IR is reduced from 28,007 instructions to one GEP, one store, and `ret i32 4000`.
- Full corpus: **507,545 -> 158,475 instructions (-68.78%)** and **1,625,882 -> 488,768 `.text` bytes (-69.94%)**.
- **Keep**: the pass removes the second dominant static-code hotspot with conservative memory invalidation. Cross-block value numbering and alias-aware memory generations remain future work.
