# ACCELA 目标校准成本调度器

## 边界与数据流

R1 将硬件事实、候选合法性、机器成本和寄存器分配压力分开。`profilec` 是 JSON 到 Java 的唯一生成入口；生成代码不得手工修改。最终编译器只链接 `GeneratedTargetProfile`，既没有 JSON 解析器，也没有运行时 Profile 覆盖入口。

R1 每次都独立构建完整生产 `FULL`，并把它作为不可变回退。`ir.schedule.deferred-r1` 是显式的替代时序候选：先推迟 LICM、两次 Unroll、StrengthReduction 与 Inlining，再由 Beam 在合法 DAG 顺序中决定执行位置；它不是空序列或隐藏 ablation。替代时序只有在 verifier、lowering、DryRunRA 和稳健支配门全部通过后才能提交，否则原样保留 `FULL`。MIR 候选同样先执行生产 Pass，再评估额外的幂等应用；成本模型拒绝候选时必须保留生产 GlobalMerge 与 MachineLICM。

```text
TargetLab raw JSON -> profilec -> target-profile.v1 JSON -> strict validate
                                                       -> generated Java -> compiler
IR/MIR candidate -> legality -> isolated snapshot -> verifier/lowering
                 -> MIR simulator + DryRunRA -> robust ranking -> commit
                                                     -> DecisionTrace JSONL
```

成本模型的输入刻意不含源文件名、路径、源码字节、case ID、期望输出、文件或 AST 指纹和测试常量标签。新输入进入调度 API 前必须通过架构审查，防止测试用例特化。

## 公共契约

- `CostEstimate` 保存 expected cycles、置信范围及 critical-path、frontend、resource、memory、branch、spill、code-size 分量。排序使用 `expected + uncertainty_weight * uncertainty`，不把不确定性隐藏在常数中。
- `LegalityResult` 只有 `PROVED` 能进入 apply；`REJECTED` 和 `UNKNOWN` 都保留基线并写明义务。
- `OptimizationCandidate` 提供稳定 ID、参数、作用区域、合法性结果和事务化 apply。
- `SchedulerPolicy` 来自嵌入 Profile。默认 Beam 宽度 8，每函数 1024 次、每模块 4096 次展开。
- `AllocationEstimate` 来自真实图着色流程：liveness、interference、simplify/coalesce、spill selection 和 coloring；在 spill rewrite 前停止。
- `PassRegistry` 与 `PipelineProfile.r1()` 固化 R1 的阶段 DAG、必需边界、候选顺序和合法性义务 ID。SSA、lowering、PHI 消除、正式 RA 和 emission 不可关闭或跨越；尚无 BOOM 排名的 12 个实验变换不在 R1 Registry 中。

任何候选都在深拷贝上执行。IR 快照重建 module、global、declaration、function、argument、block、instruction、operand 和 direct-call 映射；MIR 快照重建虚拟寄存器、块、栈槽和操作数。R1 IR Beam 在 LICM、两次小常量循环展开、整数强度削弱和 Inlining 的合法位置上保留基线与变换分支，每次变换后执行局部 cleanup、IR verifier、IR→MIR、PHI 消除、MIR verifier、成本模拟和 DryRunRA。最终状态还必须严格改善稳健总分，且 critical-path、frontend、resource、memory、branch、spill、code-size 每个分量均不得变差。变换异常、校验失败、非法配置属于编译错误，禁止静默退回旧启发式。预算耗尽不是错误：提交当前已验证最优状态，并在 trace 中记录 `budget_exhausted`。

## MIR 周期模型

每个基本块维护 `Wait/Ready/Issued` 状态、虚拟寄存器 ready time、资源下次可用周期以及当周期已发射类别。每周期最多发射 Profile 指定的 issue width；第二条必须通过对称 pairing matrix，并且资源可用。区域主体成本为：

```text
body = max(critical_path, frontend_bound, resource_bound, memory_bound)
total = body + branch + spill + code_size_penalty
```

依赖链决定 critical path；指令字节数与 fetch width 决定前端界；reciprocal throughput 和 resource occupancy 决定资源界；load/store 与 load-use 测量决定内存界。测量 MAD 传播到不确定性，稳健排序比较上界而非只比较中位数。

## Hotness 与未知循环

已知 trip count 使用精确值，并同时加权前端、依赖、资源、内存、分支及测量方差；存在非规范提前出口时不得伪装成精确循环。有限区间在端点和候选分段仿射成本交点比较。完全未知的 `N in [1,+infinity)` 只有在整个区间不劣且至少一个区间严格更优时选择，否则保留基线。实现为与块名无关的未知循环递归指令斜率向量比较：双方斜率降序匹配，候选每一项都不得更大，新增未知循环也会被拒绝。内部调用按可达调用 DAG 与调用所在块热度传播；可达递归使调用次数无界，因此替代时序 fail-closed，保留 `FULL`。这一规则由调度器、RA、Inlining 与 BlockPlacement 共享，禁止各自猜测不同热度。

## DecisionTrace v1

`--cost-trace PATH` 是显式诊断入口，默认关闭。每行是一个独立 JSON 对象，包含 schema/profile、候选及参数、合法性义务、基线和候选成本分量、置信度、DryRunRA、剪枝原因、预算使用量和最终选择。赛事 stdout 与固定调用接口不变；trace 写入失败直接终止编译。

默认开发 Profile 是通过完整质量门的 bare-metal QEMU 确定性代理测量，证据等级为 `qemu_proxy`。它会实际驱动 R1 Beam，用于验证调度闭环，但不能作为 BOOM 性能证据。未测量的 `boomv3-development.json` 只作为现场 Profile 结构模板。R2 的跨 pass 分层 Beam 只能从通过 R1 验收的提交建立，必需的 SSA、合法化、IR 到 MIR、PHI 消除、正式 RA 与汇编发射阶段不能被重排。
