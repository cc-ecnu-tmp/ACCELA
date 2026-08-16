# ACCELA r2_r1 paired report

- Evidence: `target_hardware`; formal=`false`
- Runtime metric: `seconds`
- Coverage: 6/6
- Paired GM: 1.054013
- 95% case-bootstrap CI: [0.993528, 1.123780]
- Worst case: `75_max_flow` = 0.943336
- Compile seconds median (baseline/candidate): 17.530761/94.281632
- Peak RSS bytes max (baseline/candidate): 111562752/282075136
- Code `.text` bytes median (baseline/candidate): 3202/3202
- Gate passed: `false`

Compile time and memory are reported only; they are not release limits.

## Per-case paired ratios

| Case | Baseline/candidate |
|---|---:|
| 50_short_circuit | 1.199538 |
| 65_color | 1.027189 |
| 71_full_conn | 1.081743 |
| 75_max_flow | 0.943336 |
| 85_long_code | 0.997237 |
| 87_many_params | 1.093511 |
