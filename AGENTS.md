# ACCELA Engineering Contract

## Product and target

- ACCELA is a SysY compiler targeting RISC-V RV64GC with the LP64D ABI and the
  `medany` code model. The competition objective is BOOM v3 performance without
  sacrificing functional correctness.
- The judge-facing interface is `compiler INPUT -S -o OUTPUT -O1`. Benchmark and
  diagnostic controls must use the separate development entry point; they must
  never change the default judge pipeline.
- Do not add an ARM backend, RVV lowering, or another target contract without an
  authoritative specification and an explicit project decision.

## Correctness and observability

- Fail fast on malformed benchmark manifests, unknown pass identifiers, toolchain
  drift, hash mismatches, compiler failures, invalid assembly, output mismatches,
  or incomplete metric records. Never convert these conditions into successful
  fallback runs.
- Every optimization experiment must identify the source revision, pipeline
  profile, benchmark hashes, tool versions, correctness result, and evidence
  class. Keep the transformation legality decision and rejection reason
  observable where the pass exposes candidates.
- A profile with any correctness failure is ineligible for a performance ranking.
- Started benchmark attempts are immutable, hash-bound physical evidence. Persist
  ordered phase-start, phase-result, and terminal journal events before merging
  normalized records; reject missing, orphaned, tampered, or configuration-drifted
  attempts. Only typed pre-phase/scheduler interruption may resume automatically;
  never reinterpret a real tool, timeout, runtime, or correctness failure as a retry.
  An infrastructure terminal may preserve only the journal's read-only successful
  committed prefix and original exception diagnostic; it remains ineligible for
  B1 or formal ranking and cannot resume.
- GCC/Clang reference baselines must use the hash-bound SysY source adapter and
  C++17 frontend contract recorded in the toolchain snapshot. Preserve SysY
  int32/float32 literals, additional legal identifiers, physical line numbers,
  and unmangled `main`/runtime symbols; never compile official SysY directly as C.
  Formal runs accept only the repository snapshot and fixed `python3 -I` launcher;
  its normalized `platform.python_version()` must exactly match the snapshot, and
  ambient snapshot, launcher, or image overrides are invalid provenance, while
  ambient Python module/home paths must be ignored. Probe native `docker` and
  then WSL `docker.exe`; fallback is legal
  only after a classified daemon transport/unreachable failure. The first
  reachable daemon owns the decision: missing/malformed/mismatched image evidence
  is terminal, and execution uses the exact frozen image ID. Persist normalized
  Python mode/version and Docker CLI/server-version stderr metadata. Keep the
  launcher policy in one production constant and require both the reference
  loader and campaign loader to match the snapshot exactly. The adapter
  is a lexical bridge, not a SysY parser, so formal cases must also pass manifest
  integrity and ACCELA compile/run/output correctness gates.

## Evidence levels

- `compile_only` proves only that assembly was emitted.
- `qemu_correctness` proves the local RV64GC ABI/runtime/output path.
- `qemu_proxy` may compare deterministic dynamic instructions, memory accesses,
  and the documented cache model. QEMU host wall time is not target performance.
- `boom_hardware` is required before claiming a final competition speedup or
  enabling an experimental optimization in the judge pipeline.
- Never combine identical releases of an official corpus as independent samples.
  Preserve source/input/output hashes and disclose duplicates and packaging
  defects.

## Benchmark integrity

- Optimization decisions may use semantic IR and target properties only. Never
  dispatch on file names, function names, paths, source or input hashes, benchmark
  IDs, string fingerprints, or benchmark-specific constants.
- Preserve every input byte, including unused trailing data and the exact EOF
  position. QEMU runners must expose the physical manifest input as the
  size-delimited `opt/accela/sysy-input` fw_cfg item; never append a delimiter,
  normalize bytes, or reuse UART stdin as guest input. A missing input sidecar is
  an explicit zero-byte item. Validate exact program output together with the low
  eight bits of `main`'s return value.
- Static ELF metrics may exclude only the fixed 4112-byte
  `.sysy_input_transport` NOBITS scratch; shared runtime text, rodata, and other
  state remain counted. QEMU plugins may exclude runtime I/O dynamically only
  through the exact audited helper allowlist, never by a user-matchable prefix.
- Keep downloaded corpora and raw runs under ignored directories. Commit only
  clean-room benchmarks, versioned schemas, normalized records, reproducible
  analysis, and reader-facing reports; committed records must not contain local
  absolute paths.
- Repository text is normalized to LF by `.gitattributes`. Physical SHA-256
  contracts must be computed from that checkout form; never let host
  `core.autocrlf` silently rewrite schemas, snapshots, plans, launchers, or
  source adapters.

## Change discipline

- Use a dedicated branch for performance experiments and keep unrelated changes
  out of it. New optimization algorithms belong on separate experimental
  branches and remain disabled until all correctness, holdout, QEMU, and hardware
  gates pass.
- Prefer the existing IR, analysis managers, pass manager, backend pipeline, and
  QEMU plugins over parallel frameworks. Remove obsolete experimental switches
  when the central pipeline-profile contract replaces them.
- Use incremental tests during development. Before handoff, run the affected Java
  tests, benchmark-tool tests, schema validation, deterministic metric checks,
  and at least one integer and one floating-point end-to-end RV64GC case.
