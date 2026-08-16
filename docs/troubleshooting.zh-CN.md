# 成本调度器与 TargetLab 故障诊断

按最早失败层定位，不跳过错误继续构建：

1. `doctor` 缺工具：修正显式工具链或板级文件；不联网安装、不切换容器。
2. target exit 3 或 Mailbox status 3：查看活动 metric、失败样本、baseline/measured。非正扣除和计数器倒退要求重测。
3. MAD 超限：固定频率、隔离负载、中断和温度后完整重跑九样本；不删离群点、不降低门槛。
4. Mailbox identity/length：确认 GDB 连接的是本次 ELF，检查 linker 是否保留唯一完整符号，保留 dump 和 server log。
5. Profile schema/evidence：从原始归档重新生成；不要复制 calibrated 标志或手改 evidence level。
6. embedded stale：运行 `embed` 后再 `verify-embedded`；生成 Java 不接受人工修改。
7. IR/MIR verifier：trace 指出最后候选；保存不含私有路径的最小 SysY 复现。候选异常会中止编译，不会退回旧启发式。
8. QEMU 与实机差异：QEMU 只验证协议、正确性和代理趋势。缓存、分支、pairing 或 spill 结论必须回到 BOOM 实机。

回退只允许回到上一个已经完整验证的发布提交和其匹配 Profile。不要在同一二进制里保留自动选择旧模型的隐藏路径。
