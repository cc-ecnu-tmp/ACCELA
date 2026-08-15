# TargetProfile boomv3-development-uncalibrated

- Calibrated: `false`
- Evidence level: `declared`
- Target: `rv64gc` / `lp64d` / `medany`
- Core: 50000000 Hz, issue width 2
- Measurement backend: `unmeasured`
- Timer: `unmeasured`; rdcycle=`unmeasured`; rdinstret=`unmeasured`
- Sampling: 2 warmups, 9 samples, minimum 1000000 cycles
- Measurement mode: `declared`
- SIMD enabled: `false`

## Operation measurements

| Class | Latency | MAD | Throughput | MAD |
|---|---:|---:|---:|---:|
| address | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| branch | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| call_return | 3.0000 | 0.0000 | 1.0000 | 0.0000 |
| float_alu | 3.0000 | 0.0000 | 1.0000 | 0.0000 |
| float_div | 12.0000 | 0.0000 | 1.0000 | 0.0000 |
| float_mul | 4.0000 | 0.0000 | 1.0000 | 0.0000 |
| integer_alu | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| integer_div | 12.0000 | 0.0000 | 1.0000 | 0.0000 |
| integer_mul | 3.0000 | 0.0000 | 1.0000 | 0.0000 |
| load | 4.0000 | 0.0000 | 1.0000 | 0.0000 |
| move | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| store | 1.0000 | 0.0000 | 1.0000 | 0.0000 |

## Diagnostic curves

| Metric | Point | Median | MAD | Samples |
|---|---:|---:|---:|---:|
| load_use | - | 5.0000 | 0.0000 | 1 |
| pointer_chase | - | 8.0000 | 0.0000 | 1 |
| working_set | 262144 | 8.0000 | 0.0000 | 1 |
| working_set | 32768 | 5.0000 | 0.0000 | 1 |
| working_set | 4096 | 5.0000 | 0.0000 | 1 |
| stride | 512 | 8.0000 | 0.0000 | 1 |
| stride | 64 | 5.0000 | 0.0000 | 1 |
| stride | 8 | 5.0000 | 0.0000 | 1 |
| frontend | 1024 | 128.0000 | 0.0000 | 1 |
| frontend | 256 | 32.0000 | 0.0000 | 1 |
| frontend | 64 | 8.0000 | 0.0000 | 1 |
| register_pressure | 16 | 1.0000 | 0.0000 | 1 |
| register_pressure | 24 | 17.0000 | 0.0000 | 1 |
| register_pressure | 32 | 33.0000 | 0.0000 | 1 |
| register_pressure | 8 | 1.0000 | 0.0000 | 1 |
