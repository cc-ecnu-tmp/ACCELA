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

## Linux 实机调度器配对测量

TargetLab Profile 通过校验并嵌入两侧 compiler 后，在板端使用同一份类目录、语料和 SysY runtime 运行：

```sh
taskset -c 3 python3 tools/benchmark/run_linux_paired.py \
  --baseline-classes build/classes-r1 \
  --candidate-classes build/classes-r2 \
  --corpus testsuite/functional \
  --case-list measurements/cases.txt \
  --runtime testsuite/libsysy/sylib.c \
  --output measurements/paired.tsv \
  --runs 5 \
  --warmups 2
python3 -m tools.benchmark import-linux measurements/paired.tsv measurements/results.json \
  --comparison r2_r1 --target rv64gc --abi lp64d --runtime field-board-linux
python3 -m tools.benchmark report measurements/results.json measurements/report.md
```

正式采样前应把选定核心固定在稳定频率策略，完成后恢复原 governor；策略名称和核心编号属于现场记录，不写入调度决策。每次 run 都重新启动两个 compiler，不复用编译缓存。短到接近系统调用、调页或计时器分辨率的程序仍保留原始样本并明确标注噪声，不能静默删除、改写或宣称为 BOOM 周期证据。
