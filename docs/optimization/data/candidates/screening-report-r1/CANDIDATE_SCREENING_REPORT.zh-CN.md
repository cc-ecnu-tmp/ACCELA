# ACCELA 候选算法筛选报告

> **Oracle 上界声明：** 本报告中的 GM 来自同一 FULL artifact 的 baseline／optimized Oracle 源码腿，是结构级动态指令收益上界，不是候选 Pass 的实测速率，也不是 BOOM 硬件收益。被静态合同排除的结构只作诊断，不得用于判定合格。

## 筛选终点

共有 6 个家族合格：`closed_form`、`finite_state`、`fusion`、`linear_transition`、`memoization`、`prefix_scan`。未触发“无合格项停止”条件；后续只能实现这些家族对应的显式候选。

## 合格项实施队列

该队列只决定合格项的实施先后，不是候选 Pass 实测排名。顺序固定为：最佳完整允许 Oracle GM 降序、风险升序、实现成本升序、稳定 implementation candidate ID UTF-8 字节序。

| 顺序 | 家族 | 实现候选 | 最佳完整允许 Oracle GM | 风险 | 实现成本 |
|---:|---|---|---:|---|---|
| 1 | `linear_transition` | `candidate.integer-linear-transition` | 93.1617x | `high` | `high` |
| 2 | `closed_form` | `candidate.extended-affine-summarization` | 20.6875x | `medium` | `medium` |
| 3 | `memoization` | `candidate.rrt2-on-demand-memoization` | 18.2114x | `high` | `high` |
| 4 | `prefix_scan` | `candidate.prefix-scan-reuse` | 14.5407x | `high` | `high` |
| 5 | `finite_state` | `candidate.finite-state-acceleration` | 7.0081x | `high` | `high` |
| 6 | `fusion` | `candidate.same-domain-loop-fusion` | 1.2133x | `high` | `high` |

## 证据身份与判定规则

- Screening ID：`candidate-screening-2026-r1`
- 生成时间：`2026-08-10T23:25:17.960Z`
- Candidate evidence SHA-256：`051d1ea2efeffb101fb0ace12491534b380696710cee503e0a36ce293eaee12c`
- Screening spec SHA-256：`9bc8b5855329eb0dd2b54d867c47d59113c3311e6870f33fba57a4e94adc2487`
- Oracle capture SHA-256：`319eafcdb44ddfe4258b149e3feaaa2a0479420886c9007e54942969c77366d8`
- Screening 声明的基线 PassRegistry SHA-256（`pass_registry_sha256`）：`f3175eaad399ad34b5cba3227f7f6e8f3b7f492ca50196c197b9de5d0b7496a3`
- 筛选基线 PassRegistry artifact canonical / physical SHA-256：`f3175eaad399ad34b5cba3227f7f6e8f3b7f492ca50196c197b9de5d0b7496a3` / `dd5a9ec5e8053a3fe178ebfad7a6518cb8a43b8c3b19cedc4739842f9934f1c5`。报告不记录其本地路径。
- 固定门槛：允许结构的 small／medium／large 三档全部正确完整，且三档等权动态指令 GM `>= 1.10`。
- 同时必须不存在能力重复，并具备统一 matcher／profitability／transform／proof、完整 legality obligations、精确 i32 语义且不改变 runtime／ABI。静态拒绝条件不会被 Oracle 数值覆盖。

## 十一家族总览

| 家族 | 锁定能力 | 实现候选 | 判定 | 允许结构 | 达标结构 | 现有能力重叠 | Proof | 成本 | 风险 | 精确原因 |
|---|---|---|---|---|---|---|---|---|---|---|
| `bitset` | 标量／向量位压缩 | 无 | `blocked` | 无 | 无 | 无 | `unclear` | `high` | `high` | `blocked_locked_bitset_capability_gap`（锁定的 bitset 能力存在缺口，本轮冻结为 Blocked）；`unclear_legality`（合法性证明路径不清晰） |
| `boom_ilp` | 整数 runtime partial unroll 与 reduction expansion | `candidate.integer-reduction-expansion` | `blocked` | `boom_ilp/dot_unroll4`、`boom_ilp/reduction_multi_acc` | 无 | `ir.reduction-pushdown`、`ir.loop-unroll` | `clear` | `medium` | `medium` | `oracle_structure_below_1_10`（允许结构的 Oracle GM 未达到 1.10） |
| `closed_form` | 扩展现有 SCEV／Affine 递推总结 | `candidate.extended-affine-summarization` | `qualified` | `closed_form/linear_sum`、`closed_form/quadratic_sum`、`closed_form/triangular_recurrence` | `closed_form/linear_sum`、`closed_form/quadratic_sum`、`closed_form/triangular_recurrence` | `ir.indvar-domain-simplify`、`ir.affine-loop-summarization`、`ir.strength-reduction` | `clear` | `medium` | `medium` | 无 |
| `dp_storage` | 有界依赖的滚动存储与临时表收缩 | `candidate.dp-storage-contraction` | `blocked` | `dp_storage/reverse_single_row`、`dp_storage/three_row`、`dp_storage/two_row` | 无 | `ir.sroa`、`ir.dead-store-elimination`、`ir.loop-load-elimination` | `clear` | `high` | `high` | `oracle_structure_below_1_10`（允许结构的 Oracle GM 未达到 1.10） |
| `finite_state` | 小常量闭合状态域的 jump table／binary lifting | `candidate.finite-state-acceleration` | `qualified` | `finite_state/affine_mod97`、`finite_state/branch_mod53`、`finite_state/quadratic_mod31` | `finite_state/affine_mod97`、`finite_state/branch_mod53`、`finite_state/quadratic_mod31` | `ir.affine-loop-summarization`、`ir.loop-unroll` | `clear` | `high` | `high` | 无 |
| `fusion` | 严格同域 loop fusion／temporary contraction | `candidate.same-domain-loop-fusion` | `qualified` | `fusion/single_temporary`、`fusion/stencil_producer`、`fusion/two_temporaries`、`boom_ilp/independent_chains` | `fusion/stencil_producer`、`fusion/two_temporaries`、`boom_ilp/independent_chains` | `ir.dead-store-elimination`、`ir.loop-interchange`、`ir.loop-unroll-and-jam`、`ir.loop-load-elimination` | `clear` | `high` | `high` | 无 |
| `linear_transition` | 小维度整数 affine state transition | `candidate.integer-linear-transition` | `qualified` | `linear_transition/affine_2d`、`linear_transition/affine_scalar`、`linear_transition/fibonacci_2d` | `linear_transition/affine_2d`、`linear_transition/affine_scalar`、`linear_transition/fibonacci_2d` | `ir.ranked-recurrence-tabulation`、`ir.affine-loop-summarization` | `clear` | `high` | `high` | 无 |
| `memoization` | 现有 RRT 未覆盖的 RRT2：复合 rank、可达域、按需 memo | `candidate.rrt2-on-demand-memoization` | `qualified` | `memoization/binomial`、`memoization/grid_paths` | `memoization/binomial`、`memoization/grid_paths` | `ir.ranked-recurrence-tabulation` | `clear` | `high` | `high` | 无 |
| `prefix_scan` | 重复前缀聚合的增量化复用 | `candidate.prefix-scan-reuse` | `qualified` | `prefix_scan/forward_prefix`、`prefix_scan/reverse_suffix`、`prefix_scan/weighted_prefix` | `prefix_scan/forward_prefix`、`prefix_scan/reverse_suffix`、`prefix_scan/weighted_prefix` | `ir.reduction-pushdown`、`ir.loop-strength-reduce`、`ir.loop-load-elimination` | `clear` | `high` | `high` | 无 |
| `recursion_worklist` | 混杂 tail／divide-conquer／DFS lowering | 无 | `rejected` | 无 | 无 | `ir.ranked-recurrence-tabulation`、`ir.tail-recursion-elimination` | `unclear` | `unknown` | `high` | `mixed_family_no_unified_transform`（混杂家族无法形成统一 matcher／transform／proof）；`unclear_legality`（合法性证明路径不清晰）；`unclear_specification`（候选规格不清晰） |
| `structured_kernel` | GEMM、stencil、transpose 等混杂变换 | 无 | `rejected` | 无 | 无 | `ir.loop-interchange`、`ir.loop-unroll-and-jam`、`ir.loop-unroll`、`ir.loop-load-elimination` | `unclear` | `unknown` | `high` | `mixed_family_no_unified_transform`（混杂家族无法形成统一 matcher／transform／proof）；`unclear_legality`（合法性证明路径不清晰）；`unclear_specification`（候选规格不清晰） |

## 全部 Oracle 结构与三档完整性

“允许”由锁定静态结构集合决定；不允许的结构即使 GM 较高也只能诊断。GM 仅在三档全部可排名时存在，且三档等权。

| 候选家族 | Oracle 来源 | 结构 | 静态范围 | small | medium | large | 三档完整 | Oracle GM 上界 | 门槛／用途 |
|---|---|---|---|---|---|---|---|---:|---|
| `bitset` | `bitset` | `boolean_and` | 排除 | 完整；0.8374x | 完整；0.8284x | 完整；0.8283x | 是 | 0.8314x | 诊断；静态合同排除 |
| `bitset` | `bitset` | `boolean_or` | 排除 | 完整；0.8314x | 完整；0.8316x | 完整；0.8287x | 是 | 0.8306x | 诊断；静态合同排除 |
| `bitset` | `bitset` | `boolean_xor` | 排除 | 完整；0.8098x | 完整；0.8019x | 完整；0.7984x | 是 | 0.8034x | 诊断；静态合同排除 |
| `boom_ilp` | `boom_ilp` | `dot_unroll4` | 允许 | 完整；1.0021x | 完整；1.0067x | 完整；1.0068x | 是 | 1.0052x | 完整；GM 低于 1.10 |
| `boom_ilp` | `boom_ilp` | `independent_chains` | 排除 | 完整；1.2115x | 完整；1.2142x | 完整；1.2143x | 是 | 1.2133x | 诊断；静态合同排除 |
| `boom_ilp` | `boom_ilp` | `reduction_multi_acc` | 允许 | 完整；1.0026x | 完整；1.0070x | 完整；1.0072x | 是 | 1.0056x | 完整；GM 低于 1.10 |
| `closed_form` | `closed_form` | `linear_sum` | 允许 | 完整；2.5000x | 完整；19.1154x | 完整；185.2692x | 是 | 20.6875x | 达标；可支撑家族合格 |
| `closed_form` | `closed_form` | `quadratic_sum` | 允许 | 完整；2.0345x | 完整；8.3103x | 完整；31.4828x | 是 | 8.1043x | 达标；可支撑家族合格 |
| `closed_form` | `closed_form` | `triangular_recurrence` | 允许 | 完整；2.6842x | 完整；14.4737x | 完整；108.7895x | 是 | 16.1682x | 达标；可支撑家族合格 |
| `dp_storage` | `dp_storage` | `reverse_single_row` | 允许 | 完整；1.0887x | 完整；0.9409x | 完整；0.9001x | 是 | 0.9733x | 完整；GM 低于 1.10 |
| `dp_storage` | `dp_storage` | `three_row` | 允许 | 完整；0.7401x | 完整；0.8826x | 完整；0.9467x | 是 | 0.8519x | 完整；GM 低于 1.10 |
| `dp_storage` | `dp_storage` | `two_row` | 允许 | 完整；0.8845x | 完整；0.9153x | 完整；0.9324x | 是 | 0.9105x | 完整；GM 低于 1.10 |
| `finite_state` | `finite_state` | `affine_mod97` | 允许 | 完整；0.0053x | 完整；4.2942x | 完整；428.5265x | 是 | 2.1380x | 达标；可支撑家族合格 |
| `finite_state` | `finite_state` | `branch_mod53` | 允许 | 完整；0.0141x | 完整；11.9931x | 完整；1195.1962x | 是 | 5.8666x | 达标；可支撑家族合格 |
| `finite_state` | `finite_state` | `quadratic_mod31` | 允许 | 完整；0.0175x | 完整；14.0740x | 完整；1399.2068x | 是 | 7.0081x | 达标；可支撑家族合格 |
| `fusion` | `boom_ilp` | `independent_chains` | 允许 | 完整；1.2115x | 完整；1.2142x | 完整；1.2143x | 是 | 1.2133x | 达标；可支撑家族合格 |
| `fusion` | `fusion` | `single_temporary` | 允许 | 完整；1.0784x | 完整；1.0770x | 完整；1.0769x | 是 | 1.0774x | 完整；GM 低于 1.10 |
| `fusion` | `fusion` | `stencil_producer` | 允许 | 完整；1.1257x | 完整；1.1323x | 完整；1.1325x | 是 | 1.1302x | 达标；可支撑家族合格 |
| `fusion` | `fusion` | `two_temporaries` | 允许 | 完整；1.1931x | 完整；1.1915x | 完整；1.1915x | 是 | 1.1920x | 达标；可支撑家族合格 |
| `linear_transition` | `linear_transition` | `affine_2d` | 允许 | 完整；0.0317x | 完整；1.0017x | 完整；69.8683x | 是 | 1.3048x | 达标；可支撑家族合格 |
| `linear_transition` | `linear_transition` | `affine_scalar` | 允许 | 完整；0.9344x | 完整；38.5923x | 完整；22421.6009x | 是 | 93.1617x | 达标；可支撑家族合格 |
| `linear_transition` | `linear_transition` | `fibonacci_2d` | 允许 | 完整；0.4577x | 完整；14.8669x | 完整；1030.9794x | 是 | 19.1439x | 达标；可支撑家族合格 |
| `memoization` | `memoization` | `binomial` | 允许 | 完整；1.3138x | 完整；14.0918x | 完整；326.2315x | 是 | 18.2114x | 达标；可支撑家族合格 |
| `memoization` | `memoization` | `fibonacci` | 排除 | 完整；0.6055x | 完整；0.5275x | 完整；0.5113x | 是 | 0.5466x | 诊断；静态合同排除 |
| `memoization` | `memoization` | `grid_paths` | 允许 | 完整；0.9714x | 完整；8.0457x | 完整；132.5556x | 是 | 10.1185x | 达标；可支撑家族合格 |
| `prefix_scan` | `prefix_scan` | `forward_prefix` | 允许 | 完整；1.7691x | 完整；12.6793x | 完整；94.1339x | 是 | 12.8293x | 达标；可支撑家族合格 |
| `prefix_scan` | `prefix_scan` | `reverse_suffix` | 允许 | 完整；1.8149x | 完整；12.9299x | 完整；95.8931x | 是 | 13.1042x | 达标；可支撑家族合格 |
| `prefix_scan` | `prefix_scan` | `weighted_prefix` | 允许 | 完整；1.8834x | 完整；14.7407x | 完整；110.7408x | 是 | 14.5407x | 达标；可支撑家族合格 |
| `recursion_worklist` | `recursion_worklist` | `dfs_stack` | 排除 | 完整；1.2887x | 完整；1.3723x | 完整；1.3749x | 是 | 1.3447x | 诊断；静态合同排除 |
| `recursion_worklist` | `recursion_worklist` | `divide_conquer_sum` | 排除 | 完整；5.6522x | 完整；9.5516x | 完整；9.9417x | 是 | 8.1268x | 诊断；静态合同排除 |
| `recursion_worklist` | `recursion_worklist` | `tail_to_loop` | 排除 | 完整；1.1053x | 完整；1.1840x | 完整；1.1979x | 是 | 1.1617x | 诊断；静态合同排除 |
| `structured_kernel` | `structured_kernel` | `gemm_loop_order` | 排除 | 完整；0.9607x | 完整；0.9232x | 完整；0.9047x | 是 | 0.9292x | 诊断；静态合同排除 |
| `structured_kernel` | `structured_kernel` | `stencil_boundary_split` | 排除 | 完整；0.9301x | 完整；0.8479x | 完整；0.8191x | 是 | 0.8645x | 诊断；静态合同排除 |
| `structured_kernel` | `structured_kernel` | `transpose_reduction` | 排除 | 完整；1.1225x | 完整；1.1050x | 完整；1.1018x | 是 | 1.1097x | 诊断；静态合同排除 |

## Oracle 捕获率边界

本阶段只能给出“允许结构中有多少达到上界门槛”，不能给出 Pass 捕获率。后续捕获率固定定义为 `B3 候选实测 GM / 该候选最佳允许 Oracle GM 上界`；在冻结候选 Pass 完成 B3 前一律为 `n/a`，不得把达标结构占比冒充捕获率。

| 家族 | 允许结构 | 达标结构 | 达标结构覆盖 | 最佳允许 Oracle GM 上界 | Pass 捕获率 |
|---|---:|---:|---:|---:|---:|
| `bitset` | 0 | 0 | n/a（静态阻断） | n/a | n/a（B3 未实测） |
| `boom_ilp` | 2 | 0 | 0.00% | 1.0056x | n/a（B3 未实测） |
| `closed_form` | 3 | 3 | 100.00% | 20.6875x | n/a（B3 未实测） |
| `dp_storage` | 3 | 0 | 0.00% | 0.9733x | n/a（B3 未实测） |
| `finite_state` | 3 | 3 | 100.00% | 7.0081x | n/a（B3 未实测） |
| `fusion` | 4 | 3 | 75.00% | 1.2133x | n/a（B3 未实测） |
| `linear_transition` | 3 | 3 | 100.00% | 93.1617x | n/a（B3 未实测） |
| `memoization` | 2 | 2 | 100.00% | 18.2114x | n/a（B3 未实测） |
| `prefix_scan` | 3 | 3 | 100.00% | 14.5407x | n/a（B3 未实测） |
| `recursion_worklist` | 0 | 0 | n/a（静态阻断） | n/a | n/a（B3 未实测） |
| `structured_kernel` | 0 | 0 | n/a（静态阻断） | n/a | n/a（B3 未实测） |

## 家族级实现与拒绝依据

### `bitset`

- 锁定能力：标量／向量位压缩。
- 静态处理：本轮 Blocked；不得以未冻结的位集合同进入实现队列。
- 允许／达标结构：无 / 无；最佳完整允许 Oracle GM 上界 n/a。
- 现有能力重叠：无；duplicate_of：无。
- Legality：proof path `unclear`；obligations `bitset.packed-layout`、`bitset.scalar-lowering`、`bitset.target-capability`、`bitset.observable-boundaries`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`blocked`；精确原因：`blocked_locked_bitset_capability_gap`（锁定的 bitset 能力存在缺口，本轮冻结为 Blocked）；`unclear_legality`（合法性证明路径不清晰）。

### `boom_ilp`

- 锁定能力：整数 runtime partial unroll 与 reduction expansion。
- 静态处理：排除浮点重排；independent chains 归入 fusion，不重复申报。
- 允许／达标结构：`boom_ilp/dot_unroll4`、`boom_ilp/reduction_multi_acc` / 无；最佳完整允许 Oracle GM 上界 1.0056x。
- 现有能力重叠：`ir.reduction-pushdown`、`ir.loop-unroll`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.integer-reduction-expansion.integer-only`、`candidate.integer-reduction-expansion.canonical-loop`、`candidate.integer-reduction-expansion.reduction-identity`、`candidate.integer-reduction-expansion.modulo-i32-equivalence`、`candidate.integer-reduction-expansion.trip-count-remainder`、`candidate.integer-reduction-expansion.memory-effects`、`candidate.integer-reduction-expansion.live-outs`、`candidate.integer-reduction-expansion.profitability`。
- 工程代价／风险：`medium` / `medium`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`blocked`；精确原因：`oracle_structure_below_1_10`（允许结构的 Oracle GM 未达到 1.10）。

### `closed_form`

- 锁定能力：扩展现有 SCEV／Affine 递推总结。
- 静态处理：必须复用现有递推分析，禁止另建递推框架。
- 允许／达标结构：`closed_form/linear_sum`、`closed_form/quadratic_sum`、`closed_form/triangular_recurrence` / `closed_form/linear_sum`、`closed_form/quadratic_sum`、`closed_form/triangular_recurrence`；最佳完整允许 Oracle GM 上界 20.6875x。
- 现有能力重叠：`ir.indvar-domain-simplify`、`ir.affine-loop-summarization`、`ir.strength-reduction`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.extended-affine-summarization.canonical-loop`、`candidate.extended-affine-summarization.scev-affine-state`、`candidate.extended-affine-summarization.exact-trip-count`、`candidate.extended-affine-summarization.zero-negative-iterations`、`candidate.extended-affine-summarization.modulo-i32-equivalence`、`candidate.extended-affine-summarization.side-effect-free-body`、`candidate.extended-affine-summarization.live-outs`、`candidate.extended-affine-summarization.profitability`。
- 工程代价／风险：`medium` / `medium`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`qualified`；精确原因：无。

### `dp_storage`

- 锁定能力：有界依赖的滚动存储与临时表收缩。
- 静态处理：必须统一证明依赖距离、索引边界与覆盖关系。
- 允许／达标结构：`dp_storage/reverse_single_row`、`dp_storage/three_row`、`dp_storage/two_row` / 无；最佳完整允许 Oracle GM 上界 0.9733x。
- 现有能力重叠：`ir.sroa`、`ir.dead-store-elimination`、`ir.loop-load-elimination`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.dp-storage-contraction.canonical-nest`、`candidate.dp-storage-contraction.affine-memory-access`、`candidate.dp-storage-contraction.bounded-dependence-distance`、`candidate.dp-storage-contraction.dependence-order`、`candidate.dp-storage-contraction.no-alias-or-escape`、`candidate.dp-storage-contraction.bounds-layout`、`candidate.dp-storage-contraction.observable-live-outs`、`candidate.dp-storage-contraction.profitability`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`blocked`；精确原因：`oracle_structure_below_1_10`（允许结构的 Oracle GM 未达到 1.10）。

### `finite_state`

- 锁定能力：小常量闭合状态域的 jump table／binary lifting。
- 静态处理：必须证明状态闭包、成本，以及零次／负次数语义。
- 允许／达标结构：`finite_state/affine_mod97`、`finite_state/branch_mod53`、`finite_state/quadratic_mod31` / `finite_state/affine_mod97`、`finite_state/branch_mod53`、`finite_state/quadratic_mod31`；最佳完整允许 Oracle GM 上界 7.0081x。
- 现有能力重叠：`ir.affine-loop-summarization`、`ir.loop-unroll`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.finite-state-acceleration.constant-finite-domain`、`candidate.finite-state-acceleration.transition-closure`、`candidate.finite-state-acceleration.deterministic-pure-transition`、`candidate.finite-state-acceleration.iteration-domain`、`candidate.finite-state-acceleration.exact-state-encoding`、`candidate.finite-state-acceleration.modulo-i32-equivalence`、`candidate.finite-state-acceleration.storage-code-size`、`candidate.finite-state-acceleration.profitability`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`qualified`；精确原因：无。

### `fusion`

- 锁定能力：严格同域 loop fusion／temporary contraction。
- 静态处理：优先复用 dependence、alias 与 IV 分析。
- 允许／达标结构：`fusion/single_temporary`、`fusion/stencil_producer`、`fusion/two_temporaries`、`boom_ilp/independent_chains` / `fusion/stencil_producer`、`fusion/two_temporaries`、`boom_ilp/independent_chains`；最佳完整允许 Oracle GM 上界 1.2133x。
- 现有能力重叠：`ir.dead-store-elimination`、`ir.loop-interchange`、`ir.loop-unroll-and-jam`、`ir.loop-load-elimination`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.same-domain-loop-fusion.adjacent-canonical-loops`、`candidate.same-domain-loop-fusion.identical-iteration-domain`、`candidate.same-domain-loop-fusion.dependence-order`、`candidate.same-domain-loop-fusion.alias-modref`、`candidate.same-domain-loop-fusion.side-effect-order`、`candidate.same-domain-loop-fusion.temporary-lifetime`、`candidate.same-domain-loop-fusion.exit-live-outs`、`candidate.same-domain-loop-fusion.profitability`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`qualified`；精确原因：无。

### `linear_transition`

- 锁定能力：小维度整数 affine state transition。
- 静态处理：保持精确 i32 模语义，禁止浮点重排。
- 允许／达标结构：`linear_transition/affine_2d`、`linear_transition/affine_scalar`、`linear_transition/fibonacci_2d` / `linear_transition/affine_2d`、`linear_transition/affine_scalar`、`linear_transition/fibonacci_2d`；最佳完整允许 Oracle GM 上界 93.1617x。
- 现有能力重叠：`ir.ranked-recurrence-tabulation`、`ir.affine-loop-summarization`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.integer-linear-transition.small-fixed-dimension`、`candidate.integer-linear-transition.affine-transition`、`candidate.integer-linear-transition.exact-trip-count`、`candidate.integer-linear-transition.zero-negative-iterations`、`candidate.integer-linear-transition.modulo-i32-equivalence`、`candidate.integer-linear-transition.integer-only`、`candidate.integer-linear-transition.side-effect-free-body`、`candidate.integer-linear-transition.live-outs`、`candidate.integer-linear-transition.profitability`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`qualified`；精确原因：无。

### `memoization`

- 锁定能力：现有 RRT 未覆盖的 RRT2：复合 rank、可达域、按需 memo。
- 静态处理：不得重复申报普通 Fibonacci tabulation。
- 允许／达标结构：`memoization/binomial`、`memoization/grid_paths` / `memoization/binomial`、`memoization/grid_paths`；最佳完整允许 Oracle GM 上界 18.2114x。
- 现有能力重叠：`ir.ranked-recurrence-tabulation`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.rrt2-on-demand-memoization.direct-finite-recursion`、`candidate.rrt2-on-demand-memoization.composite-well-founded-rank`、`candidate.rrt2-on-demand-memoization.pure-context`、`candidate.rrt2-on-demand-memoization.reachable-domain`、`candidate.rrt2-on-demand-memoization.memo-key-injective`、`candidate.rrt2-on-demand-memoization.base-case-order`、`candidate.rrt2-on-demand-memoization.modulo-i32-equivalence`、`candidate.rrt2-on-demand-memoization.bounded-storage`、`candidate.rrt2-on-demand-memoization.no-abi-runtime-change`、`candidate.rrt2-on-demand-memoization.profitability`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`qualified`；精确原因：无。

### `prefix_scan`

- 锁定能力：重复前缀聚合的增量化复用。
- 静态处理：必须证明无别名、无副作用与空域语义。
- 允许／达标结构：`prefix_scan/forward_prefix`、`prefix_scan/reverse_suffix`、`prefix_scan/weighted_prefix` / `prefix_scan/forward_prefix`、`prefix_scan/reverse_suffix`、`prefix_scan/weighted_prefix`；最佳完整允许 Oracle GM 上界 14.5407x。
- 现有能力重叠：`ir.reduction-pushdown`、`ir.loop-strength-reduce`、`ir.loop-load-elimination`；duplicate_of：无。
- Legality：proof path `clear`；obligations `candidate.prefix-scan-reuse.repeated-prefix-domain`、`candidate.prefix-scan-reuse.incremental-equivalence`、`candidate.prefix-scan-reuse.integer-order`、`candidate.prefix-scan-reuse.alias-modref`、`candidate.prefix-scan-reuse.side-effect-free-kernel`、`candidate.prefix-scan-reuse.empty-domain`、`candidate.prefix-scan-reuse.bounds`、`candidate.prefix-scan-reuse.live-outs`、`candidate.prefix-scan-reuse.profitability`。
- 工程代价／风险：`high` / `high`；规格 `clear`；BOOM 特性依赖 `false`。
- 最终判定：`qualified`；精确原因：无。

### `recursion_worklist`

- 锁定能力：混杂 tail／divide-conquer／DFS lowering。
- 静态处理：家族没有统一 matcher／transform／proof，整族拒绝且不拆票。
- 允许／达标结构：无 / 无；最佳完整允许 Oracle GM 上界 n/a。
- 现有能力重叠：`ir.ranked-recurrence-tabulation`、`ir.tail-recursion-elimination`；duplicate_of：无。
- Legality：proof path `unclear`；obligations `recursion_worklist.unified-kernel`、`recursion_worklist.well-foundedness`、`recursion_worklist.side-effect-order`、`recursion_worklist.stack-bound`、`recursion_worklist.observable-order`。
- 工程代价／风险：`unknown` / `high`；规格 `unclear`；BOOM 特性依赖 `false`。
- 最终判定：`rejected`；精确原因：`mixed_family_no_unified_transform`（混杂家族无法形成统一 matcher／transform／proof）；`unclear_legality`（合法性证明路径不清晰）；`unclear_specification`（候选规格不清晰）。

### `structured_kernel`

- 锁定能力：GEMM、stencil、transpose 等混杂变换。
- 静态处理：家族没有统一 matcher／transform／proof，整族拒绝且不拆票。
- 允许／达标结构：无 / 无；最佳完整允许 Oracle GM 上界 n/a。
- 现有能力重叠：`ir.loop-interchange`、`ir.loop-unroll-and-jam`、`ir.loop-unroll`、`ir.loop-load-elimination`；duplicate_of：无。
- Legality：proof path `unclear`；obligations `structured_kernel.unified-kernel`、`structured_kernel.dependence-order`、`structured_kernel.alias-modref`、`structured_kernel.boundary-semantics`、`structured_kernel.cost-model`。
- 工程代价／风险：`unknown` / `high`；规格 `unclear`；BOOM 特性依赖 `false`。
- 最终判定：`rejected`；精确原因：`mixed_family_no_unified_transform`（混杂家族无法形成统一 matcher／transform／proof）；`unclear_legality`（合法性证明路径不清晰）；`unclear_specification`（候选规格不清晰）。

## 重复候选审计

没有声明重复候选组。

## 结论边界

排序若存在，仅用于合格项实施队列；Oracle GM 是上界而非 Pass 实测。本报告不构成候选默认启用、BOOM v3 收益或竞赛最终加速声明。
