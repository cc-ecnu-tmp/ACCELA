# Candidate evaluation

ACCELA uses one evaluator with no campaign contracts, hashes, ledgers, receipts, or audit chain.
It compiles, links, executes, checks exact output, records QEMU dynamic instructions, and ranks
the six experiment candidates over B2--B6.  The active profile directory contains only
`baseline.json` plus one profile for each of `candidate.sysy-region-memory-forwarding`,
`candidate.function-specialization`, `candidate.array-object-promotion`,
`candidate.nested-address-recurrence`, `candidate.cost-model-loop-tiling`, and
`candidate.rv64-word-pressure`; all six are default-off and are enabled only by
`BenchmarkCompiler` profiles.

Run from a Linux-native checkout:

```sh
python3 scripts/evaluate_candidates.py --max-runs 6 --jobs 4
```

The evaluator builds the compiler and QEMU plugins, runs up to six profiles concurrently and
four cases per profile, resumes already-passed cases, then writes `summary.json` and `REPORT.md`
under `.tmp/simple-evaluation`. B3 also evaluates the three pair profiles formed from its Top 3
single candidates. A complete B2–B6 run then performs the greedy combination pass and a distinct
final verification profile. Failed cases remain in the report and make that profile unrankable;
they are never silently skipped. Use `--no-diagnostics` to skip the B3 pairs and
`--skip-build` only when the current checkout has already been built.
Pass `--environment-label Ubuntu-Dev-RH2288V3` only on the authenticated dedicated host; other
labels produce provisional metrics and cannot authorize integration.

Required tools are JDK 21, Python 3.11+, `riscv64-elf-gcc`, `riscv64-elf-readelf`,
`qemu-system-riscv64` with plugin headers, GLib 2, `pkg-config`, and a C compiler.
QEMU dynamic-instruction counts are proxy measurements and must not be described as BOOM
hardware speedups.  A candidate is rankable only when every case compiles, links, runs, matches
the complete output (including the uint8 return trailer), and produces a valid instruction count.
Promotion additionally requires B3 geometric mean > 1, combined B3--B6 geometric mean > 1, no
stage below 0.99, and an improvement over the current production combination.
The preliminary B4 `fft0` entry is excluded because its bundled 100+100 input is paired with
an unrelated 99,999-element expected output. `if-combine2` and `if-combine3` are also excluded:
both programs call `getint` twice, but their bundled inputs contain only one integer. `03_sort1`
is excluded because its 30,000,010-element global array produces a 120 MB guest image whose
startup exceeds the 30-minute QEMU proxy limit for every profile.

## Dedicated evaluation machine

Create the upload archive from a Linux checkout containing both official suites:

```sh
sh scripts/package-evaluation.sh
```

Upload `accela-evaluation.tar.zst`, then run on the authenticated Ubuntu evaluation machine.
Before measuring, verify CPU, memory, disk, Docker, toolchain, and the checkout filesystem; stop
if any prerequisite is unavailable.  Run the formal measurement from a Linux-native filesystem,
never from DrvFS/9p:

```sh
mkdir accela-evaluation
tar --zstd -xf accela-evaluation.tar.zst -C accela-evaluation
cd accela-evaluation
docker compose build
MAX_RUNS=6 JOBS=4 docker compose up --abort-on-container-exit
```

One global progress bar covers all single and B3 pair cases. Results remain on the host in `results/`.
Package them and copy the archive back:

```sh
tar --zstd -cf accela-results.tar.zst -C results .
# Run this from the development machine:
scp USER@EVALUATION_HOST:/path/to/accela-evaluation/accela-results.tar.zst .
```
