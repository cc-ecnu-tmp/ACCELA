# DecisionTrace v1 解读

`--cost-trace PATH` 是唯一显式启用入口，默认关闭。输出是 UTF-8 JSONL，每行独立、确定、可流式处理；写入或关闭失败会让编译失败。赛事 stdout 和汇编输出不包含 trace。

关键字段：

- `profile` 与 `profile_evidence`：嵌入 Profile ID 和证据等级。
- `candidate`、`target_kind`、`target_name`：稳定候选与 IR/MIR 区域；不含源路径或 case ID。
- `legality` 与 `legality_obligation`：`proved` 才能进入成本比较；`not_applicable`、`unknown`、`rejected` 不变换。
- `baseline`、`transformed`：cycles、uncertainty、critical path、frontend、resources、memory、branch、spill、code size。
- `allocation`：真实 DryRunRA 的 predicted spills、spill weight、峰值 integer/float live 和 coalescing loss。
- `expansions`、`expansion_budget`：可复现预算使用；`budget_exhausted` 是正常停止，不是异常 fallback。

R1 IR Beam 的最终行使用 `candidate: ir.beam.final`，`parameters.sequence` 给出提交的合法候选序列。中间 `considered` 只表示候选已验证并计价，不表示最终提交。MIR GlobalMerge 和 MachineLICM 各自记录最终 applied/rejected。固定输入和 Profile 的 trace 必须逐行一致。
