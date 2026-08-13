# ACCELA clean-room benchmark corpus

This directory contains deterministic, source-available SysY benchmarks and
semantic oracle pairs used to evaluate optimization candidates. All sources in
this directory are clean-room originals licensed under MIT; names describe
algorithm families only and do not imply source compatibility with upstream
benchmark suites.

Taxonomy names come from the [PolyBench/C project](https://www.cs.colostate.edu/~pouchet/software/polybench/polybench.html)
and [Embench-IoT](https://github.com/embench/embench-iot). ACCELA uses only
their public workload names and algorithm behavior as research context. It does
not copy or translate upstream source, inputs, constants, or reference output.
The generated corpus itself is SPDX `MIT`; see `LICENSE`.

The primary corpus has exactly:

- 14 PolyBench-shaped programs: 2mm, 3mm, atax, bicg, gemm, gemver, gesummv,
  mvt, dynprog, durbin, jacobi-1d, jacobi-2d, seidel-2d, and fdtd-2d.
- 8 Embench-shaped programs: aha-mont64, crc32, huffbench, matmult-int,
  nettle-aes, nettle-sha256, statemate, and wikisort.
- 11 optimization-candidate families with three structurally distinct
  baseline/optimized semantic-oracle pairs per family.
- 60 clean-room structural variants covering 20 locked family taxonomies, with
  CFG/induction-equivalent, small deterministic, and large different
  deterministic roles for each taxonomy.

Every primary program has a minimal `correctness` dataset plus `small`,
`medium`, and `large` performance datasets. Every oracle pair has three scaling
datasets. Programs print a compact checksum; checked-in expected outputs store
the exact stdout bytes followed by process exit code 0. Validation separates
the development interpreter's framing newline from program stdout and never
normalizes whitespace.

Run `python benchmarks/generate.py --check` to verify checked-in generated
sources, datasets, independently cross-checked binary32 reference outputs, and
the manifest. Run
`python -m unittest discover -s benchmarks/tests -v` for corpus invariants and
the independent 32-bit integer/float32 semantic checks.

See METHODOLOGY.md for evidence boundaries and validation modes, and
RESEARCH_MAPPING.md for the old-conversation versus current-corpus mapping.
