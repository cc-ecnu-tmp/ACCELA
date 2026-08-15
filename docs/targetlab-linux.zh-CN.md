# TargetLab Linux 后端

Linux 后端构建普通 RV64GC/LP64D 可执行文件，并由显式 `execute` 命令传到目标环境运行。`execute` 可以是板端本地包装器、受控 SSH 包装器或 QEMU user-mode；TargetLab 不解释隐含主机配置，也不会寻找默认设备。

## 配置与能力探测

```sh
python -m tools.targetlab configure \
  --backend linux \
  --cc riscv64-linux-gnu-gcc \
  --objcopy riscv64-linux-gnu-objcopy \
  --nm riscv64-linux-gnu-nm \
  --clock-hz 50000000 \
  --minimum-cycles 1000000 \
  --measurement-mode hardware \
  --execute './field/run-on-target.sh' \
  --output targetlab-linux.json
python -m tools.targetlab doctor targetlab-linux.json
```

程序用 SIGILL 防护分别执行 `rdcycle` 和 `rdinstret`。`rdcycle` 不可访问时，计时源显式变为 `clock_gettime(CLOCK_MONOTONIC)`；环境记录、原始单位和最终 Profile 必须一致。切换不是静默 fallback，报告会保留实际来源。`rdinstret` 不可用不阻止 wall-clock 测量，但能力必须记录为 `false`。

## 运行与证据

```sh
python -m tools.targetlab build targetlab-linux.json
python -m tools.targetlab run targetlab-linux.json --output raw.jsonl
python -m tools.targetlab collect raw.jsonl collected.json
python -m tools.targetlab profile collected.json \
  config/target/boomv3-development.json target-profile.json \
  --profile-id field-board-linux-2026-08
```

`raw.jsonl` 第一行是唯一环境记录，其余每行是唯一 metric。每项保留 iterations、normalization、九个 baseline、九个 measured 和九个扣除归一化值。目标退出码、stderr 的活动 metric 和失败样本是定位计数器倒退或非正扣除的权威诊断，不能只保留最终 Profile。

QEMU user-mode 只能用 `--measurement-mode qemu_proxy`。TCG 的 `rdcycle` 可能受主机调度影响；MAD 或 baseline 门拒绝数据是预期的 fail-closed 行为，不得提升为硬件证据。
