# R1 QEMU TCG 无退化修复与复测

## 问题

首次完整 functional 配对测量覆盖 100 个公开样例，每个配置实际执行五次。成本调度版本的动态指令比 `FULL` 的几何均值为 `0.884989`，95% case-bootstrap 区间为 `[0.842674, 0.924469]`；43 例回退，`65_color` 在 120 秒内无输出并超时。

DecisionTrace 显示多数回退样例选择了空 IR Beam 序列。检查实现后确认，Compiler 使用了移除 LICM、两次 Unroll、StrengthReduction 和 Inlining 的特殊 PassBuilder；空序列实际代表 `FULL - R1 passes`，不是生产 `FULL`。BackendPipeline 还会在成本模型拒绝 GlobalMerge 或 MachineLICM 时移除原本必跑的 MIR Pass。这违反 `FULL` 对 `FULL + candidate` 的评测契约。

## 修复

- 删除可构造隐藏 R1 ablation 的 PassBuilder API，Compiler 始终先运行完整生产 `FULL`。
- IRCandidateScheduler 只在完整 `FULL` 之后事务化探索额外候选。
- BackendPipeline 先执行生产 GlobalMerge 和 MachineLICM，再让调度器评估额外的幂等应用；候选被拒绝不能删除生产行为。
- 架构测试断言 PassBuilder 不再暴露布尔 ablation 构造器。

## QEMU TCG 结果

修复后重新覆盖全部 100 个公开 functional 样例。每个候选实际执行五次，逐次校验输出；原版数据来自同一 QEMU TCG 环境中保存的五次配对测量，首次缺失完整基线的 `65_color` 重新执行五次。

- 证据等级：`qemu_proxy`
- 指标：目标动态指令数，速度比方向为 `FULL / (FULL + scheduler)`
- 覆盖率：`100/100`
- 配对几何均值：`1.000000`
- 95% case-bootstrap 区间：`[1.000000, 1.000000]`
- 改善 / 不变 / 回退：`0 / 100 / 0`
- 最差单例：`1.000000`
- `65_color`：双方均为 `43,088,369` 条动态指令并正确结束

这关闭了当前 QEMU TCG 公开集上的退化，但没有证明收益，也不能通过 BOOM 发布门禁。`qemu_proxy` 只能作为诊断证据；R1 仍需 BOOM v3 上的独立五次冷启动配对测量。
