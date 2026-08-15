# TargetProfile JSON v1 规范

## 编码与严格性

文件必须是 UTF-8 JSON，顶层必须且只能包含：`schema_version`、`profile`、`target`、`measurement_environment`、`scheduler`、`operations`、`pairing`、`branch`、`spills`、`diagnostics`、`simd`。`schema_version` 固定为 `1`。未知字段、缺失字段、非有限数、负成本、非法宽度、单位混用、矩阵不完整或非对称均立即失败。

`profile.id` 是稳定标识；只有现场原始样本通过质量门槛后，`profilec` 才生成 `calibrated: true`。`profile.evidence_level` 只能是 `declared`、`qemu_proxy` 或 `target_hardware`，必须与校准状态及测量模式一致；复制、生成或嵌入不能提升证据等级。手填或声明型开发数据必须为 `false` 和 `declared`。

`target` 声明 ISA、ABI、code model、频率以及 fetch/issue/retire 宽度。`measurement_environment` 记录实际后端、`rdcycle`/`rdinstret` 可用性、最终计时源、测量频率、最小周期、warmup、正式样本数和测量模式；未校准开发 Profile 使用 `unmeasured` 与 `null`，校准 Profile 不允许保留未测值。R1 正式默认是 `rv64gc`、`lp64d`、`medany`、双发射 BOOM v3。`simd` 保留 ISA extension、ABI、register class 和 benchmark class 注册点；正式 ISA/ABI 和许可未发布前必须 `enabled:false`、两个文本字段为 `null`、两个注册表为空，不得填造成本。

## 测量对象与单位

每个 measurement 必须包含 `median`、`mad`、`sample_count`、`source`。内部单位是 cycle；样本数是扣除 empty kernel 后保留的正式样本数；source 必须与 TargetLab 报告一致。原始 JSON 使用整数缩放单位 `rdcycle_x1000` 或 `clock_gettime_ns_x1000`，避免跨语言浮点文本差异。后者必须结合 `target.clock_hz` 转成 cycle，降级不得静默发生。

算术、吞吐和 pairing 要求 `MAD/median <= 0.01`；branch、memory、working-set 和 spill 转折允许 `<= 0.05`。样本不足 9、计数器倒退、empty 扣除后非正或超过门槛均拒绝 Profile；不丢弃离群样本。

校准归档必须恰好覆盖注册表中的全部指标。每项保留 `iterations`、每次 baseline、每次 measured、归一化因子和九个扣除后值，并与 `measurement_environment.timer` 的单位一致。`profilec` 逐项重算 `(measured - baseline) * 1000 / (iterations * normalization)`；任一不一致立即失败。上三角 pairing 每项只测一次，由 `profilec` 镜像成完整对称矩阵；缺项、重复项或额外项都不会继承开发 Profile 的占位值。

## 指令类与矩阵

`operations` 必须完整包含 `integer_alu`、`integer_mul`、`integer_div`、`float_alu`、`float_mul`、`float_div`、`load`、`store`、`branch`、`call_return`、`address`、`move`。每类包含 latency、throughput、resource occupancy、code bytes 和资源名。

`pairing` 是以上类别的完整方阵。`pairing[a][b]` 与 `pairing[b][a]` 必须是相同测量，不能只填上三角或用默认值补洞。值表示两类独立指令成对发射的 reciprocal throughput。

`scheduler` 保存 Beam 和展开预算。修改现场 JSON 后必须重新执行 validate、embed 和 verify-embedded；最终程序不提供命令行覆盖。

## 生成与一致性

```sh
python -m tools.targetlab validate target-profile.json
python -m tools.targetlab embed target-profile.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
python -m tools.targetlab verify-embedded target-profile.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
```

`verify-embedded` 按确定性生成结果逐字节检查。差异表示源 Profile 与编译器不一致，必须停止构建；禁止编辑生成 Java 来绕过检查。
