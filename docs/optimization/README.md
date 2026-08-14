# Candidate evaluation

ACCELA uses one evaluator with no campaign contracts, hashes, ledgers, receipts, or audit chain.
It compiles, links, executes, checks exact output, records QEMU dynamic instructions, and ranks
the six implemented candidates over B2--B6.

Run from a Linux-native checkout:

```sh
python3 scripts/evaluate_candidates.py --max-runs 6 --jobs 4
```

The evaluator builds the compiler and QEMU plugins, runs up to six profiles concurrently and
four cases per profile, resumes already-passed cases, then writes `summary.json` and `REPORT.md`
under `.tmp/simple-evaluation`. B3 also evaluates the three pair profiles formed from its Top 3
single candidates. Use `--no-diagnostics` to skip those pairs and `--skip-build` only when the
current checkout has already been built.

Required tools are JDK 21, Python 3.11+, `riscv64-elf-gcc`, `riscv64-elf-readelf`,
`qemu-system-riscv64` with plugin headers, GLib 2, `pkg-config`, and a C compiler.
QEMU results are proxy measurements and must not be described as BOOM hardware speedups.
The preliminary B4 `fft0` entry is excluded because its bundled 100+100 input is paired with
an unrelated 99,999-element expected output.

## Dedicated evaluation machine

Create the upload archive from a Linux checkout containing both official suites:

```sh
sh scripts/package-evaluation.sh
```

Upload `accela-evaluation.tar.zst`, then run on the 64-thread evaluation machine:

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
