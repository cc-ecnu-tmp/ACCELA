# R1 QEMU TCG 调度收益与无退化复测

## 问题

首次完整 functional 配对测量覆盖 100 个公开样例，每个配置实际执行五次。成本调度版本的动态指令比 `FULL` 的几何均值为 `0.884989`，95% case-bootstrap 区间为 `[0.842674, 0.924469]`；43 例回退，`65_color` 在 120 秒内无输出并超时。

DecisionTrace 显示多数回退样例选择了空 IR Beam 序列。检查实现后确认，Compiler 使用了移除 LICM、两次 Unroll、StrengthReduction 和 Inlining 的特殊 PassBuilder；空序列实际代表 `FULL - R1 passes`，不是生产 `FULL`。BackendPipeline 还会在成本模型拒绝 GlobalMerge 或 MachineLICM 时移除原本必跑的 MIR Pass。这违反 `FULL` 对 `FULL + candidate` 的评测契约。

## 修复

- 删除可构造隐藏 R1 ablation 的 PassBuilder API，Compiler 始终独立运行完整生产 `FULL` 并保留为不可变回退。
- 将推迟 R1 盈利 Pass 的路径建模为显式 `ir.schedule.deferred-r1` 时序候选；候选序列不得为空，且只有通过事务校验和稳健支配门才可替换 `FULL`。
- BackendPipeline 先执行生产 GlobalMerge 和 MachineLICM，再让调度器评估额外的幂等应用；候选被拒绝不能删除生产行为。
- 架构测试断言 PassBuilder 不再暴露布尔 ablation 构造器。
- 模块成本按可达调用 DAG 传播调用频次；可达递归调用图拒绝替代时序，消除了递归样例的长尾回退。
- 精确循环次数同时加权前端与后端成本；提前出口不冒充精确 trip count。完全未知循环使用与块名无关的递归指令斜率向量门，修复 `95_float` 中把循环不变量重新放回循环体的回退。

## QEMU TCG 结果

修复后重新覆盖全部 100 个公开 functional 样例。每个候选实际执行五次，逐次校验输出；原版数据来自同一 QEMU TCG 环境中保存的五次配对测量，首次缺失完整基线的 `65_color` 重新执行五次。

- 证据等级：`qemu_proxy`
- 指标：目标动态指令数，速度比方向为 `FULL / (FULL + scheduler)`
- 覆盖率：`100/100`
- 配对几何均值：`1.005657`
- 95% case-bootstrap 区间：`[1.001403, 1.011271]`
- 改善 / 不变 / 回退：`8 / 92 / 0`
- 最差单例：`1.000000`
- `65_color`：双方均为 `43,088,369` 条动态指令并正确结束

改善样例为 `56_sort_test2`、`63_big_int_mul`、`73_int_io`、`80_chaos_token`、`81_skip_spaces`、`83_long_array`、`87_many_params` 和 `88_many_params2`；单例动态指令比分别为 `1.011364`、`1.090344`、`1.182599`、`1.023174`、`1.034826`、`1.130729`、`1.117064`、`1.007968`。

这证明当前 QEMU TCG 动态指令代理上，调度版本相对原版 `FULL` 的配对 GM 与置信下界均超过 1.00，且没有单例回退；它仍不能通过 BOOM 发布门禁。`qemu_proxy` 只能作为诊断证据，不能证明真实周期、缓存、分支预测或双发射收益；R1 仍需 BOOM v3 上的独立五次冷启动配对测量。
