# 中断批次

这些 `.partial` 文件是未纳入汇总的原始诊断，不是完整结果：

- `appended-newline-input.tsv.partial`：发现 runner 会在语料输入后额外追加换行后主动停止。根因已修复为逐字节传递输入。
- `pre-boundary-cost-fix.tsv.partial`：运行期间发现无 PHI 函数跨过必需 PHI 消除边界时没有立即初始化 Beam 成本。修复并增加回归测试后废弃该批次；连接中断也验证了 `.partial` 与 governor 恢复路径。
- `nondeterministic-r1.tsv.partial`：严格的跨 run 汇编比较在 `69_expr_eval` 捕获到 R1 标签分配不确定。根因是 LICM promotion 迭代 identity set/map；改为函数块顺序与插入顺序后，R1 连续 6 次、R2 连续 4 次生成相同汇编。

二者都保留已完成行，不进入 results JSON、GM 或置信区间。
