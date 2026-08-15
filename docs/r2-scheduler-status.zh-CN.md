# R2 分层成本调度器

## 实现状态

R2 在 R1 的不可变 `FULL` 回退之上运行，已经接入默认编译路径。当前实现完成了除 BOOM v3 实机校准、赛事 LLVM 配对测量和由该证据决定的实验 Pass 晋级之外的软件侧目标。这里的“完成”不等于发布：没有 BOOM 证据时，R2 仍是候选，不能宣称超越 LLVM。

`R2PassRegistry.production()` 是 71 个生产 occurrence 的唯一注册表，覆盖全部现有 IR Pass、IR→MIR、MIR cleanup、PHI 消除、正式寄存器分配、分支折叠、两次块布局和汇编发射。每项具有稳定 ID、阶段、scope、依赖、分析需求、分析失效集合、必需标记和合法性义务。注册表可确定性导出 `pass-registry.r2.v1` JSON，不向成本接口暴露文件名、路径、case ID、输出或源码指纹。

## 分层 Beam

IR 层从原始、已验证的 IR 快照开始。每个可选 occurrence 产生 `APPLY` 与 `SKIP` 事务分支；必需的 `mem2reg` 只有 `APPLY`。每次 apply 都在深拷贝上使用新分析管理器执行，随后运行 IR verifier、IR→MIR lowering、PHI 消除、MIR verifier、目标成本模拟和真实图着色流程的 DryRunRA。只有严格改善稳健分数、所有成本分量均不差、未知循环斜率不差且可达调用图非递归的完整状态，才能替代 R1 `FULL`。

MIR 层按函数运行。PHI 前的状态可以参与合法顺序搜索，但不会提前调用 liveness 或 DryRunRA；必需 PHI 消除完成后，所有状态才进入目标成本和 DryRunRA 排序。正式 RA 在选中 pre-RA 快照安装到原函数后恰好执行一次，AllocationResult 因而始终属于最终函数。分支折叠和 post-RA block placement 的四种组合在已分配克隆上比较，再将选中的布尔计划应用到正式分配结果。

IR 和 MIR Beam 均使用 Profile 内嵌的宽度与展开预算。预算耗尽会记录 `budget_exhausted`，把 Beam 中每个前缀沿 DAG 完成到合法状态，并在已验证状态中选择稳健支配 `FULL` 的最优项；没有合格项时保留 `FULL`。配置非法、Pass 异常、分析缺失、registry/executor 漂移或 verifier 失败会中止编译，不会静默切回旧启发式。

## DAG 与分析边界

大部分依赖边保留生产顺序。当前仅放宽三个已验证窗口：两处 `EarlyCSE`/`InstSimplify` 清理窗口，以及 PHI 后的 `MemoryAddressFolding`/`MachineCSE` 窗口。Beam 会实际枚举这些窗口的两个拓扑顺序。窗口后的 occurrence 同时依赖窗口两端，避免越过汇合点。

任何新放宽都必须同时满足：

1. 不跨 IR、lowering、MIR、RA 或 emission 阶段；
2. 所需分析可重新计算，失效集合在 registry 中明确；
3. 两个顺序均通过事务 apply 后 verifier、固定输入确定性和功能差分；
4. 成本比较保持 fail-closed，不以模型分数替代正确性证据。

必需边界为 `ir.mem2reg.1`、`backend.ir-lowering.1`、`backend.phi-elimination.1`、`backend.register-allocation.1` 和 `backend.asm-emission.1`。BackendPipeline 会在运行时核对所有 MIR occurrence 均有执行器；新增 registry 项但未接线会立即失败。

## DecisionTrace

显式 `--cost-trace PATH` 才启用 JSONL。R2 逐分支记录 occurrence、参数、合法性义务、基线/候选成本向量、DryRunRA、展开数、预算和剪枝原因，并以 `r2.ir.beam.final`、`r2.mir.beam.final` 汇总最终选择。lowering、RA 和 emission 等固定边界也记录 `required_boundary`。默认 stdout 和赛事调用接口不变。

## 验收边界

软件验收依次包括 Java 单元/架构测试、Target Profile 生成一致性、TargetLab 双后端测试、固定输入汇编与 trace 确定性、SysY/QEMU 输出差分，以及 R1/R2 五次配对的 QEMU 动态指令代理。QEMU 结果只能证明代理环境中的正确性和无退化，不能证明 BOOM 周期收益。

12 个实验变换继续不在生产 registry 中。它们必须逐个通过合法性、`FULL + candidate`、组合、长尾和 BOOM 实测后才能晋级；由于本阶段明确排除 BOOM v3，R2 不会提前注册这些变换。最终发布仍要求相对组委会 LLVM 的 BOOM 配对 GM 95% 置信下界大于 1.00，且任何样例不低于 0.90。
