# VisionFive 2 U74 Linux 测量记录（2026-08-16）

本目录保存 R2 开发阶段的非 BOOM 实机证据。目标为四核 SiFive U74、RV64GC、LP64D、`medany`、最高 1.5 GHz；Profile 测量固定单核执行。用户态实测不能访问 `rdcycle`/`rdinstret`，TargetLab 明确记录后使用 `clock_gettime`，没有静默降级。该证据等级是 `target_hardware`，只适用于此目标的校准和无退化诊断，不能代替 BOOM v3 或赛事 LLVM 验收。

## Profile 证据

`profile/` 保存配置、模板、完整 JSONL 原始样本、收集结果、通过校验的 Profile 和报告。每项先预热 2 次、正式采样 9 次，kernel 自动放大到至少一千万等效 cycle；121 项均保留 baseline、measured 和扣除后的样本。Profile ID 为 `visionfive2-u74-linux-10m-20260816`。

第一次以一百万等效 cycle 测量的原始证据保存在 `rejected-1m/`。生成器按设计拒绝该批次：`operations.integer_alu.latency` 的 `MAD/median=0.012085`，超过算术项 `0.01` 门槛。没有删样本、放宽阈值或把失败批次标成可用。

可从保存的收集结果重新生成并验证：

```sh
python3 -m tools.targetlab profile \
  measurements/visionfive2-u74-linux-20260816/profile/collected.json \
  measurements/visionfive2-u74-linux-20260816/profile/template.json \
  build/visionfive2-target-profile.json \
  --profile-id visionfive2-u74-linux-10m-20260816
python3 -m tools.targetlab validate build/visionfive2-target-profile.json
python3 -m tools.targetlab embed \
  build/visionfive2-target-profile.json build/visionfive2-generated
```

## R1/R2 比较边界

相同 VisionFive 2 Profile 分别嵌入 R1 与 R2 后，公开 functional 语料 100/100 均可编译，其中 94 个用例汇编逐字节一致，6 个不同用例列在 `comparison/different-assembly-cases.txt`。这 6 个用例的 QEMU TCG 五次动态指令配对结果保存在 `comparison/qemu-proxy/`：GM 1.005142、95% case-bootstrap CI `[1.000000, 1.015505]`、最差 1.000000。其余 94 个只能由“汇编相同”推导动态行为相同；QEMU 指令数仍只是代理证据。

VisionFive 2 上的预热 AB/BA 五次 wall-clock 配对结果保存在 `comparison/target-hardware/`：GM 1.054013、95% case-bootstrap CI `[0.993528, 1.123780]`、最差 `75_max_flow` 为 0.943336。全部程序运行只有毫秒级，结果受进程启动、调页和计时噪声影响；其中最差用例两侧汇编目标 `.text` 字节完全相同，QEMU 动态指令也相同，因此该 wall-clock 差值不是已证实的代码退化。报告仍保留全部样本，不删除最差值，也不把非 BOOM 实机数据升级成正式发布证据。
