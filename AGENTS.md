# ACCELA Engineering Contract

## Product and target

- ACCELA compiles SysY to RISC-V RV64GC with LP64D and the `medany` code model.
- The judge interface remains `compiler INPUT -S -o OUTPUT -O1`.
- The final compiler embeds one validated `target-profile.v1` JSON profile and never discovers a runtime profile.

## Cost scheduling

- TargetProfile, TargetLab, compiler cost classes, and DecisionTrace share stable instruction-class names.
- Only a profile explicitly marked `calibrated: true` may change profitability decisions. The checked-in development profile preserves the existing production pipeline.
- Candidate matchers do not receive filenames, paths, source bytes, case identifiers, expected outputs, hashes, or test fingerprints.
- Legality is fail-closed: only `PROVED` candidates may transform code; `REJECTED` and `UNKNOWN` remain unchanged and observable.
- Candidate evaluation is transactional. Internal errors and verifier failures abort compilation; they never select a fallback candidate silently.
- Search limits are deterministic expansion budgets. Budget exhaustion selects the best already-validated state and emits `budget_exhausted`.

## Evidence

- BOOM hardware measurements are the only evidence that can close the LLVM performance gate.
- QEMU, static cost estimates, host smoke tests, and Oracle programs are diagnostic evidence, not BOOM or release evidence.
- R1 release requires correctness, paired GM lower confidence bound at least 1.00 versus ACCELA FULL, and no case below 0.90.
- R2 release additionally requires paired GM lower confidence bound greater than 1.00 versus the organizer LLVM baseline.
- Experimental transformations are measured as `FULL` versus `FULL + candidate`; `without.*` is diagnostic only.

## Field measurement

- TargetLab is offline, assembly-level, and independent of ACCELA code generation.
- JSON inputs are strict: unknown fields, incomplete pairing matrices, invalid units, unstable critical measurements, and malformed mailboxes fail immediately.
- Every result records its timer source. `rdcycle` to `clock_gettime` fallback is explicit and retained in the generated profile.
- Do not run TargetLab at an event until a human has confirmed generic hardware microbenchmarks are permitted.

## Change discipline

- Use a dedicated branch for optimization and scheduler work.
- Preserve the fixed judge interface and unrelated worktree contents.
- Update architecture, field, and acceptance documentation with behavior changes.
- Run affected Java tests, TargetLab self-tests, embedded-profile verification, and `git diff --check` before handoff.
