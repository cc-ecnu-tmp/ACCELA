# Benchmark methodology and evidence boundaries

SPDX-License-Identifier: MIT

## Provenance

Every SysY source in this directory was written from an algorithm-family
description without consulting or translating upstream benchmark source.
PolyBench and Embench names are used only as workload taxonomy. Inputs,
constants, checksums, control flow, and reference implementations are ACCELA
clean-room originals. The corpus contains no official competition input,
filename fingerprint, input hash, function-name hash, or machine-local path.

The taxonomy references are the [PolyBench/C project](https://www.cs.colostate.edu/~pouchet/software/polybench/polybench.html)
and [Embench-IoT](https://github.com/embench/embench-iot). Only public names and
algorithm behavior were used; no upstream source was copied or translated.
All files generated for this corpus are SPDX `MIT` under `LICENSE`.

The structural-variant corpus uses only the locked family names as opaque
taxonomy labels. For each of its 20 taxonomies, the generator emits exactly
three clean-room sources: a CFG/induction-equivalent form, a small deterministic
form, and a larger independently seeded form. The manifest records
role=structural_variant, taxonomy, variant kind, provenance, and seed.

The AES-shaped program implements byte substitution, row permutation, and
GF(2^8) column mixing from first principles. The SHA-256-shaped program
preserves the compression-loop and boolean/data-dependency shape using explicit
16-bit lanes because source-level SysY has neither unsigned integers nor
bitwise operators. Neither program claims wire-format compatibility with the
named library benchmark.

## Reproducibility

manifest.json is the single source of truth for the fixed primary/oracle seed,
the independently namespaced structure-variant seed, source paths, dataset
sizes, per-dataset seeds, roles, and reference-output paths. generate.py derives
every primary/oracle seed from SHA-256 of the public corpus seed, benchmark
identifier, and size tier; structural variants use the structure-variant seed
instead. This derivation is only for deterministic corpus generation; compiler
optimizations must never use benchmark identities or these seeds.

Reference arithmetic is independent Python:

- SysY integers use signed 32-bit two's-complement wrap after each operation.
- Integer division truncates toward zero; the INT_MIN / -1 machine result is
  represented explicitly.
- Float references round to IEEE-754 binary32 after every source-level
  operation. A `struct.pack` implementation and an independently written
  `ctypes.c_float` implementation must agree bit-for-bit at every result before
  an output is emitted. Float outputs are then quantized to deterministic
  integer checksums.

Regenerate artifacts with:

    python benchmarks/generate.py

Verify that checked-in artifacts are current without writing:

    python benchmarks/generate.py --check
    python -m unittest discover -s benchmarks/tests -v

After the compiler classes have been built, parse every SysY source:

    python benchmarks/validate.py --parse

Run the dedicated correctness tier through the ACCELA interpreter:

    python benchmarks/validate.py --interpret-correctness

Run every primary-program tier or every oracle-pair tier explicitly:

    python benchmarks/validate.py --interpret-primary-all
    python benchmarks/validate.py --interpret-oracles-all

Primary datasets have four explicit roles: one minimal `correctness` input and
three `performance` inputs (`small`, `medium`, and `large`). Oracle inputs retain
three scaling tiers. Interpreter validation compares stdout byte-for-byte and
compares the low eight bits of `main`'s return independently; whitespace is not
tokenized or normalized.

## Oracle interpretation

Each oracle pair proves only that its baseline and optimized source programs
have the same checked outputs on the generated datasets. It is not a proof that
the compiler implements the optimization, nor that a transformation is legal
for arbitrary programs. The three pairs in each family deliberately vary data
shape or control/dependence structure to discourage one-pattern overfitting.

The source-level bitset oracles use arithmetic bit packing because i64 and
bitwise operators are unavailable in SysY. They validate representation
equivalence, not the expected performance of compiler-private RV64 word
packing.

## Performance evidence

Correctness, parser acceptance, interpreter execution, host-native execution,
QEMU instruction counts, BOOM simulation, and final-board timing are distinct
evidence classes. Do not infer BOOM speedup from reference generation or
interpreter success. Performance reports should record at least:

- compiler revision, flags, target configuration, and dataset tier;
- warm-up/repetition policy and timeout/failure handling;
- wall time, dynamic instructions, loads/stores, and emitted code size;
- per-program baseline/optimized ratios and weighted geometric mean;
- optimization remarks or other evidence that a candidate actually matched.

No benchmark-specific tuning is permitted. Thresholds may depend on generic IR
features and target properties, then must be frozen before evaluation on a
held-out corpus.
