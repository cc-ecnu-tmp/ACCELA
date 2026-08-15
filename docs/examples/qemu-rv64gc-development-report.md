# TargetProfile qemu-rv64gc-baremetal-proxy-v1

- Calibrated: `true`
- Evidence level: `qemu_proxy`
- Target: `rv64gc` / `lp64d` / `medany`
- Core: 50000000 Hz, issue width 2
- Measurement backend: `baremetal`
- Timer: `rdcycle`; rdcycle=`true`; rdinstret=`true`
- Sampling: 2 warmups, 9 samples, minimum 1000000 cycles
- Measurement mode: `qemu_proxy`
- SIMD enabled: `false`

## Operation measurements

| Class | Latency | MAD | Throughput | MAD |
|---|---:|---:|---:|---:|
| address | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| branch | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| call_return | 2.0000 | 0.0000 | 2.0000 | 0.0000 |
| float_alu | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| float_div | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| float_mul | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| integer_alu | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| integer_div | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| integer_mul | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| load | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| move | 1.0000 | 0.0000 | 1.0000 | 0.0000 |
| store | 1.0000 | 0.0000 | 1.0000 | 0.0000 |

## Diagnostic curves

| Metric | Point | Median | MAD | Samples |
|---|---:|---:|---:|---:|
| load_use | - | 2.0000 | 0.0000 | 9 |
| pointer_chase | - | 1.0000 | 0.0000 | 9 |
| working_set | 262144 | 4.0000 | 0.0000 | 9 |
| working_set | 32768 | 4.0000 | 0.0000 | 9 |
| working_set | 4096 | 4.0000 | 0.0000 | 9 |
| stride | 512 | 4.0000 | 0.0000 | 9 |
| stride | 64 | 4.0000 | 0.0000 | 9 |
| stride | 8 | 4.0000 | 0.0000 | 9 |
| frontend | 1024 | 251.0000 | 0.0000 | 9 |
| frontend | 256 | 59.0000 | 0.0000 | 9 |
| frontend | 64 | 11.0000 | 0.0000 | 9 |
| register_pressure | 16 | 16.0000 | 0.0000 | 9 |
| register_pressure | 24 | 24.0010 | 0.0000 | 9 |
| register_pressure | 32 | 48.0020 | 0.0000 | 9 |
| register_pressure | 8 | 8.0000 | 0.0000 | 9 |
