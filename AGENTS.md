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
- Candidate addition is the primary optimization path: evaluate one named,
  reviewable compiler change against a hash-bound unchanged baseline before
  composing qualified candidates. A disable-profile or `without.*` run is
  diagnostic evidence about existing code only; it cannot select, rank, or
  stand in for a newly implemented candidate.
- Candidate execution and campaign evidence must bind pass-registry v2 and
  pipeline-profile v2; the static candidate-evidence inventory alone does not.
  Preserve two distinct immutable registry identities: screening binds the
  pre-implementation zero-candidate base export, while profiles and campaigns bind a
  separately named post-implementation executable export. The executable
  non-candidate projection must equal the base in full and in order; its candidate IDs
  and legality obligations must exactly equal the qualified screening/catalog set.
  Before implementation, run one FULL baseline and one FULL optimized execution
  over the frozen 99-pair Oracle plan and produce one capture indexed by
  family, structure, and size. At least one mapped structure must have complete,
  correct small/medium/large observations whose equal-weight three-size
  geometric-mean speedup is at least 1.10.
  Define speedup only as baseline dynamic instructions divided by candidate
  dynamic instructions. B2 is a tuning pass for every B1-correct candidate, not
  an elimination gate; freeze all tuned candidates before B3. Only a B3
  geometric mean strictly greater than 1 advances to B4--B6. Rank eligible
  single candidates by the equal-weight combined geometric mean over all 267
  B3--B6 cases, then B3 geometric mean, smaller static text bytes, and stable
  candidate ID. The current campaign has no BOOM run and cannot enable a
  candidate by default.
- A profile with any correctness failure is ineligible for a performance ranking.
- The speed-first candidate campaign is a separate evidence class and campaign
  namespace. It may import only the sealed normalized B1 and B2 FULL records of
  an older campaign through a dual-revision bootstrap whose changed paths are
  restricted to orchestration code and do not overlap measurement components.
  It must never claim raw-ledger continuity across revisions.
- Fast campaign authorization consumes one immutable plan/status/run-index
  snapshot and checks task membership in a bounded ready wave. It must not
  reopen historical raw attempt trees or status ledgers. Keep at most four
  concurrent formal runs and exactly four case workers per run. Coordinate each
  task, output, run state, and mutable current-head update with OS-backed locks;
  lock files are active-writer metadata, not historical evidence.
- Every fast run still preserves the normal fail-fast attempt journal and sealed
  normalized run record. Publish an immutable, plan-bound terminal receipt before
  advancing the append-only run index. Later status, study, audit, and final
  consumers may read only normalized run records and receipts. They must reject
  hash, configuration, tool, profile, protocol, manifest, baseline, correctness,
  metric-coverage, or repository drift without falling back to the legacy path.
- Run bounded normalized-record audits at bootstrap, after B2, after B3, and
  before final publication. B2 evaluates every candidate. B3 promotes only an
  all-correct, ranking-eligible candidate with geometric-mean speedup strictly
  greater than 1.0; B4--B6 must evaluate exactly that promoted subset. Record
  non-promoted task cancellation against the B3 study rather than fabricating a
  run receipt.
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
- Under Docker Desktop, start the campaign container from the exact frozen
  candidate-toolchain image, give a custom container the same name and hostname,
  mount the exact labeled campaign volume read-write, and expose the Docker socket.
  The reference launcher must verify its outer container/image, embedded Linux
  Docker CLI path/hash/version, and the unique effective workspace volume. Inner
  GCC/Clang containers use validated `volume-subpath` mounts; host execution keeps
  the existing bind-mount transport. Ambiguity, nested mount shadowing, label/name
  drift, path traversal, a missing socket, or another image is terminal.

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
- Run formal campaigns only from Linux-native storage. A native WSL filesystem
  is valid; under Docker Desktop the repository, corpora, compiler build, raw
  attempts, status ledgers, and reports must all live in one Linux named volume.
  Never run a formal campaign from a Windows checkout exposed through a bind
  mount, DrvFS, or 9p. Treat one clean, hash-equivalent Linux checkout as the sole
  campaign workspace, copy rescued inputs into it with physical hash verification,
  and rebuild location-bound virtual environments and build outputs there. A
  campaign migrated after an infrastructure failure must use a new campaign ID,
  plan, and run namespace; never combine evidence across workspace identities,
  repository revisions, campaign IDs, or the aborted predecessor. Export results
  only after the campaign is terminal and revalidate canonical and physical hashes.
- Repository control text is normalized to LF by `.gitattributes`, while the
  tracked `testsuite` corpus is explicitly byte-preserving because its committed
  line endings are part of benchmark identity. Physical SHA-256 contracts must
  be computed from that checkout form; never let host `core.autocrlf` silently
  rewrite schemas, snapshots, plans, launchers, source adapters, or corpus bytes.
- A campaign stopped because its evaluation direction is invalid must be frozen
  under a versioned, hash-bound diagnostic manifest. Preserve its registered
  terminal records and committed journal prefix, but never resume it, append a
  synthetic terminal, promote from it, or import any of its measurements into a
  candidate campaign. A replacement campaign requires a new campaign ID, plan,
  run IDs, and candidate identities; only independently hash-verified corpus,
  protocol, and toolchain assets may be reused.

## Change discipline

- Use a dedicated branch for performance experiments and keep unrelated changes
  out of it. Each new or materially changed optimization candidate belongs on a
  separate experimental branch and remains disabled until its baseline-paired
  correctness, holdout, QEMU, and hardware gates pass.
- Prefer the existing IR, analysis managers, pass manager, backend pipeline, and
  QEMU plugins over parallel frameworks. Remove obsolete experimental switches
  when the central pipeline-profile contract replaces them.
- Use incremental tests during development. Before handoff, run the affected Java
  tests, benchmark-tool tests, schema validation, deterministic metric checks,
  and at least one integer and one floating-point end-to-end RV64GC case.
