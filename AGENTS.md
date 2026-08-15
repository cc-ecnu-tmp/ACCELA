# ACCELA Engineering Contract

## Product and target

- ACCELA compiles SysY to RISC-V RV64GC with LP64D and the `medany` code model.
- The judge interface remains `compiler INPUT -S -o OUTPUT -O1`.
- The final compiler embeds one validated `target-profile.v1` JSON profile and never discovers a runtime profile.

## Cost scheduling

- TargetProfile, TargetLab, compiler cost classes, and DecisionTrace share stable instruction-class names.
- Only a profile explicitly marked `calibrated: true` may change profitability decisions. `evidence_level` distinguishes declared, QEMU-proxy, and target-hardware data; QEMU data never inherits hardware evidence.
- Candidate matchers do not receive filenames, paths, source bytes, case identifiers, expected outputs, hashes, or test fingerprints.
- Legality is fail-closed: only `PROVED` candidates may transform code; `REJECTED` and `UNKNOWN` remain unchanged and observable.
- Candidate evaluation is transactional. Internal errors and verifier failures abort compilation; they never select a fallback candidate silently.
- Search limits are deterministic expansion budgets. Budget exhaustion selects the best already-validated state and emits `budget_exhausted`.
- `PassRegistry` and `PipelineProfile.r1()` are the single source for R1 pass identity, phase order, dependencies, required boundaries, and legality obligation IDs. The 12 unranked experimental transforms are absent.
- R1 scheduling is strictly additive: the initial state is the complete production `FULL` pipeline, and candidates are evaluated only as `FULL + candidate`. There is no API for constructing a hidden R1-pass ablation; rejecting an IR or MIR candidate must retain production LICM, Unroll, StrengthReduction, Inlining, GlobalMerge, and MachineLICM behavior.

## Evidence

- BOOM hardware measurements are the only evidence that can close the LLVM performance gate.
- QEMU, static cost estimates, host smoke tests, and Oracle programs are diagnostic evidence, not BOOM or release evidence.
- The embedded development profile is measured with deterministic bare-metal QEMU proxy kernels. It exercises the calibrated scheduler but is not BOOM calibration or release evidence.
- R1 release requires correctness, paired GM lower confidence bound at least 1.00 versus ACCELA FULL, and no case below 0.90.
- R2 release additionally requires paired GM lower confidence bound greater than 1.00 versus the organizer LLVM baseline.
- Experimental transformations are measured as `FULL` versus `FULL + candidate`; `without.*` is diagnostic only.

## Field measurement

- TargetLab is offline, assembly-level, and independent of ACCELA code generation.
- JSON inputs are strict: unknown fields, incomplete pairing matrices, invalid units, unstable critical measurements, and malformed mailboxes fail immediately.
- Every result records its timer source. `rdcycle` to `clock_gettime` fallback is explicit and retained in the generated profile.
- A calibrated profile requires the complete registry, exactly nine positive raw samples per metric, a measured counter environment, and exact symmetric pairing data. Partial archives never inherit development values.
- Run `python -m tools.targetlab selftest` and `doctor` before field measurement. Preserve raw JSONL, Mailbox dumps, OpenOCD logs, and failure status.
- Do not run TargetLab at an event until a human has confirmed generic hardware microbenchmarks are permitted.

## Change discipline

- Use a dedicated branch for optimization and scheduler work.
- Preserve the fixed judge interface and unrelated worktree contents.
- Update architecture, field, and acceptance documentation with behavior changes.
- Run affected Java tests, TargetLab self-tests, embedded-profile verification, and `git diff --check` before handoff.
