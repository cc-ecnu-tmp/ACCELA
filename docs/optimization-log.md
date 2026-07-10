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

## 2026-07-11 — Boolean PHI diamond folding

### Research and hypothesis

- The new largest case, `h_functional/29_long_line.sy`, retained 887 PHIs and 1,776 branches after optimization. Most are diamonds that assign integer 0/1 from a dynamic condition.
- LLVM `SimplifyCFG.cpp` if-converts eligible two-entry PHIs to `select`. ACCELA has no `select`, but a 0/1 PHI can be represented exactly as `zext i1`; reversed values use `xor i1 condition, true` before the extension.

### Implementation

- Added a focused SimplifyCFG fold for a merge with exactly two predecessors: one conditional predecessor and one empty forwarding block.
- The fold requires an `i32` PHI with opposite constant 0/1 incoming values and an indirect block whose only predecessor is the conditional block. Other PHIs and diamonds are left unchanged.
- After PHI replacement, the existing fixed-point CFG cleanup removes the empty block and now-redundant conditional branch.
- Added direct/inverted unit tests and a dynamic-input microbenchmark.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Microbenchmark: **49 -> 39 instructions**. `h_functional/29_long_line.sy`: **33,362 -> 27,771 instructions (-16.76%)**; `fib` falls from 1,775 to 685 IR blocks.
- Full corpus: **158,475 -> 152,849 instructions (-3.55%)** and **488,768 -> 472,304 `.text` bytes (-3.37%)**.
- **Keep**: a narrow, source-independent CFG canonicalization with measurable corpus-wide benefit. General select-based if-conversion remains future work.

## 2026-07-11 — Direct RISC-V comparisons with zero

### Research and hypothesis

- `h_functional/29_long_line.sy` still emitted 6,952 `li` instructions. The common `icmp ne x, 0` sequence became `li 0; sub; snez` under the generic two-register comparison lowering.
- LLVM RISC-V patterns select equality with zero as `sltiu x, 1` (`seqz`) and inequality as `sltu zero, x` (`snez`); signed relations can name the architectural `zero` register directly.

### Implementation

- When an integer comparison's right operand is constant zero, lowering now emits direct `seqz`, `snez`, or `slt ... zero` forms. `sle`/`sge` invert the corresponding strict comparison with `xori`.
- Other constants and register-register comparisons retain the existing path.
- Added assembly regression coverage for all six supported integer predicates and for absence of zero materialization/subtraction.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Boolean microbenchmark: **39 -> 35 instructions**. `h_functional/29_long_line.sy`: **27,771 -> 25,447 instructions (-8.37%)**.
- Full corpus: **152,849 -> 148,433 instructions (-2.89%)** and **472,304 -> 459,240 `.text` bytes (-2.77%)**.
- **Keep**: exact target instruction selection, broad corpus benefit, and no IR semantic change. General signed-12-bit immediate selection is the next extension.

## 2026-07-11 — Signed-12-bit arithmetic immediates

### Research and implementation

- LLVM RISC-V patterns select register-plus-signed-12-bit constants as `ADDI` and register XOR constants as `XORI`.
- ACCELA now emits `addi` for `add x, imm` and `sub x, imm` when the negated subtraction constant fits, plus `xori` for XOR. Commutative add/XOR move a left-side immediate to the encodable right side.
- Constants outside `[-2048, 2047]`, division, remainder, and multiplication retain register-register lowering.
- Added assembly tests for positive/negative immediates, commuted operands, and absence of redundant `li` plus register operations.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- `h_functional/29_long_line.sy`: **25,447 -> 25,355 instructions**.
- Full corpus: **148,433 -> 147,649 instructions (-0.53%)** and **459,240 -> 457,638 `.text` bytes (-0.35%)**.
- **Keep**: modest but broad target-level improvement with a small, exact encoding check.

## 2026-07-11 — Signed-12-bit comparison immediates

### Research and implementation

- LLVM RISC-V uses `SLTI` directly for signed less-than immediates and rewrites other predicates through inversion or an adjusted bound.
- ACCELA now selects `slti` for `< C`, inverts it for `>= C`, and uses `C + 1` for `<= C`/`> C` when the adjusted constant is encodable.
- Equality predicates use `addi x, -C` followed by `seqz`/`snez`. Every rewrite checks signed-12-bit range and overflow boundaries; otherwise generic register comparison remains.
- Assembly tests cover all six predicates and confirm that encodable constants do not create `li` plus register comparisons.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Full corpus: **147,649 -> 147,157 instructions (-0.33%)** and **457,638 -> 456,182 `.text` bytes (-0.32%)**.
- **Keep**: small distributed improvement with exact immediate-range guards.

## 2026-07-11 — Fused integer compare branches

### Research and hypothesis

- LLVM RISC-V has direct branch-condition patterns mapping integer `setcc` users to `BEQ`, `BNE`, `BLT`, and `BGE` (with operand swaps for complementary predicates).
- ACCELA previously materialized every `icmp`, spilled its boolean result, reloaded it in `condbr`, then emitted `bnez`. This is unnecessary when the compare result has only that branch use.

### Implementation

- The assembly printer counts Machine IR virtual-register uses. An `ICMP` is fused only when immediately followed by `CONDBR` on its result and the result has exactly one use.
- Target emission maps all six signed integer predicates to the four RISC-V branch forms, swaps operands for `>`/`<=`, and uses architectural `zero` without materialization.
- Comparisons with other uses, non-adjacent branches, and floating comparisons keep the existing value-producing path.
- Regression tests exercise all six predicates and verify that no `setcc`/`bnez` sequence remains.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Boolean microbenchmark: **35 -> 34 instructions**. `h_functional/29_long_line.sy`: **25,351 -> 22,628 instructions (-10.74%)**.
- Full corpus: **147,157 -> 138,922 instructions (-5.60%)** and **456,182 -> 429,780 `.text` bytes (-5.79%)**.
- **Keep**: this is a guarded Machine IR combine, not a textual assembly peephole, and removes a broad all-spill amplification pattern.

## 2026-07-11 — Pre-allocation compare branch fusion

### Hypothesis and implementation

- The first fusion ran in the assembly printer, after all-spill allocation. Although compare instructions were no longer emitted, their virtual registers still consumed stack slots and could push spill offsets outside the 12-bit addressing range.
- Added an explicit Machine IR pass after PHI elimination and before register allocation. It rewrites the conditional branch to carry the compare predicate and operands, then removes the single-use `ICMP`.
- The RISC-V rewriter now emits this fused Machine IR form directly. The old late printer scan and entry point were deleted.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- `h_functional/29_long_line.sy`: **22,628 -> 22,239 instructions** from smaller frames and fewer large-offset spill sequences.
- Full corpus: **138,922 -> 137,262 instructions (-1.20%)** and **429,780 -> 424,726 `.text` bytes (-1.18%)**.
- **Keep**: same guarded fusion semantics, but at the correct Machine IR phase with measurable allocation-side benefit.

## 2026-07-11 — Interference-aware spill-slot coloring

### Research and hypothesis

- ACCELA's allocator assigned every virtual register a unique stack slot. Large functions therefore exceeded RISC-V's signed-12-bit load/store offset range, turning each spill into `li; add; load/store`.
- Standard register allocation uses liveness-derived interference: values that are never live simultaneously may occupy the same location. The same rule safely applies to stack-slot coloring before physical-register coloring exists.

### Implementation

- Added an undirected interference graph from every instruction's live-before and live-after sets.
- The all-spill allocator greedily reuses an existing slot only for the same Machine IR type and only when the new virtual register interferes with none of that slot's occupants.
- A MOVE whose source and destination were coalesced to the same stack slot is omitted; different stack slots and physical registers retain the normal move path.
- Tests prove boundary-touching live ranges can share, simultaneously live ranges cannot, same-slot moves vanish, and different-slot moves remain.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Slot coloring alone: **137,262 -> 98,484 instructions (-28.25%)**, `.text` **424,726 -> 280,254 bytes (-34.01%)**; `long_line` **22,239 -> 8,897**.
- Coalesced MOVE elimination: **98,484 -> 95,076 instructions (-3.46%)**, `.text` **280,254 -> 272,706 bytes (-2.69%)**; `long_line` **8,897 -> 8,081**.
- **Keep**: a large, general reduction derived from exact CFG liveness. Dynamic RISC-V execution remains unavailable, so runtime claims are still deferred.

## 2026-07-11 — Optimistic caller-saved graph coloring

### Research and design

- Chaitin's simplify/select allocator established interference-graph coloring; Briggs showed that high-degree nodes can be removed optimistically and spilled only if select cannot color them.
- George/Appel interleave simplify, coalesce, and freeze. ACCELA starts with the smaller Briggs core and defers coalescing until base allocation is stable.
- LLVM's greedy allocator explicitly checks call regmasks before assignment. ACCELA similarly excludes values live across `CALL` or a lowered `memset` libcall from its caller-saved-only color set.

### Implementation

- Added independent optimistic simplify/select coloring for integer and floating-point classes. Potential spills use a cost/degree choice; only select failures become actual spills.
- The initial safe colors are non-scratch caller-saved registers: `t4-t6` and `ft4-ft11`. Cross-call values retain the existing stack-slot allocation until callee-saved save/restore exists.
- Tests cover optimistic coloring, uncolorable graphs, call-clobber boundaries, distinct colors for interference, and spill-slot behavior after coloring.

### Validation and decision

- Unit tests: pass. Full optimized-IR correctness: **140/140 pass**; full ACCELA RISC-V assembly: **140/140 assemble**.
- Full corpus: **95,076 -> 91,407 instructions (-3.86%)** and **272,706 -> 249,878 `.text` bytes (-8.37%)**.
- **Keep**: allocation is correct and reduces memory traffic, but allocated operands still pass through scratch-register moves. Direct physical-register emission is the next isolated layer.
