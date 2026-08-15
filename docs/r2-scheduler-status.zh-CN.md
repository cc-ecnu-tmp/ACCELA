# R2 分层调度器状态与边界

## 当前阶段

R2 从 R1 已验证提交建立独立候选分支。R1 的 `FULL` 不可变回退、TargetProfile、MIR 成本模拟、DryRunRA、IR/MIR 快照、DecisionTrace 和 QEMU 无退化门继续有效。R2 尚未获得 BOOM 实机验收，因此当前完成度只代表架构启动，不代表 R2 发布或超越 LLVM。

`R2PassRegistry.production()` 冻结当前生产流水线的 71 个 occurrence，覆盖：

- 早期 SSA 与标量清理；
- module/global、Inlining 前后 cleanup；
- induction、loop、GVN、LICM、Unroll 与 StrengthReduction；
- IR→MIR、PHI 消除及 MIR cleanup；
- block placement、正式寄存器分配、分支折叠和汇编发射。

每个 occurrence 具有稳定 ID、family、IR/MIR 阶段、function/module scope、必需标记、依赖边以及 required/invalidated analyses。注册表可确定性导出 `pass-registry.r2.v1` JSON，供 DecisionTrace、现场诊断和评测工具使用，不包含文件名、路径、case ID 或源码指纹。

## Fail-closed DAG

首个 R2 DAG 有意保留当前生产顺序，先关闭“漏 Pass、重复 occurrence、跨阶段和分析失效”风险，再逐边放宽可证明安全的重排。不能为了扩大搜索空间删除依赖边；只有为两个 occurrence 补齐分析前置条件、失效规则、合法性义务和 verifier 测试后，才能允许新的拓扑顺序。

`R2ScheduleState` 是不可变 Beam 决策前缀。每个 occurrence 必须明确选择 `APPLY` 或 `SKIP`；必需 occurrence 禁止跳过，依赖未决时禁止执行。`R2AnalysisState` 在 apply 前检查分析是否有效，并在 apply 后显式清除 invalidated analyses。分析不足、非法阶段或不完整 profile 都立即报错，不进入成本排序。

必需边界为：

1. `ir.mem2reg.1`
2. `backend.ir-lowering.1`
3. `backend.phi-elimination.1`
4. `backend.register-allocation.1`
5. `backend.asm-emission.1`

## 后续接入顺序

1. 让 PassBuilder 和 BackendPipeline 从 occurrence 注册表构造并核对生产流水线，消除人工双写。
2. 将 R1 硬编码 IR Spec 替换为 occurrence factory/provider。
3. 在 Beam state 中加入 IR/MIR snapshot、成本向量、分析状态、合法序列和 trace pruning。
4. 在保持阶段边界的前提下，逐窗口放宽 DAG 边并执行组合搜索。
5. 12 个实验变换继续默认关闭；逐个通过合法性、`FULL + candidate`、组合与 BOOM 数据后再注册。
6. QEMU 只用于正确性和代理筛选；最终以同机 BOOM 上赛事 LLVM/ACCELA 配对结果执行 R2 发布门。
