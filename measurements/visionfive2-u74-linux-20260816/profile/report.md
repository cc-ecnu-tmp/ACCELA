# TargetProfile visionfive2-u74-linux-10m-20260816

- Calibrated: `true`
- Evidence level: `target_hardware`
- Target: `rv64gc` / `lp64d` / `medany`
- Core: 1500000000 Hz, issue width 2
- Measurement backend: `linux`
- Timer: `clock_gettime`; rdcycle=`false`; rdinstret=`false`
- Sampling: 2 warmups, 9 samples, minimum 10000000 cycles
- Measurement mode: `hardware`
- SIMD enabled: `false`

## Operation measurements

| Class | Latency | MAD | Throughput | MAD |
|---|---:|---:|---:|---:|
| address | 0.9870 | 0.0000 | 0.4980 | 0.0015 |
| branch | 2.0085 | 0.0015 | 2.0145 | 0.0000 |
| call_return | 6.0750 | 0.0015 | 6.0765 | 0.0030 |
| float_alu | 5.0355 | 0.0000 | 2.5170 | 0.0000 |
| float_div | 9.0840 | 0.0015 | 8.1000 | 0.0030 |
| float_mul | 5.0340 | 0.0015 | 2.5185 | 0.0015 |
| integer_alu | 0.9870 | 0.0000 | 0.4980 | 0.0015 |
| integer_div | 68.8080 | 0.0255 | 68.8050 | 0.0180 |
| integer_mul | 3.0135 | 0.0015 | 1.5060 | 0.0015 |
| load | 0.9870 | 0.0000 | 0.9990 | 0.0000 |
| move | 0.9870 | 0.0015 | 0.4965 | 0.0000 |
| store | 0.9870 | 0.0000 | 0.9990 | 0.0015 |

## Diagnostic curves

| Metric | Point | Median | MAD | Samples |
|---|---:|---:|---:|---:|
| load_use | - | 1.0125 | 0.0030 | 9 |
| pointer_chase | - | 2.0550 | 0.0060 | 9 |
| working_set | 262144 | 17.4120 | 0.0060 | 9 |
| working_set | 32768 | 1.0485 | 0.0060 | 9 |
| working_set | 4096 | 1.0095 | 0.0015 | 9 |
| stride | 512 | 18.5100 | 0.0090 | 9 |
| stride | 64 | 17.4150 | 0.0075 | 9 |
| stride | 8 | 3.0735 | 0.0045 | 9 |
| frontend | 1024 | 250.9935 | 0.0405 | 9 |
| frontend | 256 | 56.7375 | 0.0465 | 9 |
| frontend | 64 | 8.0985 | 0.0045 | 9 |
| register_pressure | 16 | 7.0875 | 0.0045 | 9 |
| register_pressure | 24 | 11.1330 | 0.0015 | 9 |
| register_pressure | 32 | 47.5980 | 0.0075 | 9 |
| register_pressure | 8 | 2.0250 | 0.0015 | 9 |
