# ACCELA r2_r1 paired report

- Evidence: `qemu_proxy`; formal=`false`
- Runtime metric: `instructions`
- Coverage: 6/6
- Paired GM: 1.005142
- 95% case-bootstrap CI: [1.000000, 1.015505]
- Worst case: `65_color` = 1.000000
- Compile seconds median (baseline/candidate): 4.871088/8.110755
- Peak RSS bytes max (baseline/candidate): 543961088/1240203264
- Code `.text` bytes median (baseline/candidate): 1496/1496
- Gate passed: `false`

Compile time and memory are reported only; they are not release limits.

## Per-case paired ratios

| Case | Baseline/candidate |
|---|---:|
| 50_short_circuit | 1.031250 |
| 65_color | 1.000000 |
| 71_full_conn | 1.000000 |
| 75_max_flow | 1.000000 |
| 85_long_code | 1.000000 |
| 87_many_params | 1.000000 |
