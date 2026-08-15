# TargetLab QEMU 双后端验证记录

日期：2026-08-15。工具：riscv64-elf-gcc 15.2.0、riscv64-linux-gnu-gcc 15.1.0、QEMU 11.1.0、GDB 17.2。目标契约：RV64GC、LP64D、medany、50 MHz 归一化、warmup 2、正式样本 9。

## Bare-metal system QEMU

managed QEMU debug server 启动成功；GDB 通过 `targetlab_mailbox` 符号断点和对象范围导出 88,160 字节 Mailbox。Header、121 个注册 metric、344 字节 entry、完整 raw evidence、对称 pairing、Profile 校验、Java embed、verify-embedded 和报告生成均通过。生成的内置 Profile 是 `qemu-rv64gc-baremetal-proxy-v1`，证据等级明确为 `qemu_proxy`。

## RISC-V Linux user QEMU

Linux ELF 构建、动态加载、SIGILL 保护的 `rdcycle/rdinstret` 能力探测、JSONL 环境记录、逐 metric flush、121 项完整归档和 collect 均通过。100,000,000 cycle 目标下，`operations.integer_alu.latency` 的 `MAD/median=0.011848`，超过算术类固定上限 `0.010000`，因此 profile 阶段按设计拒绝。后续重复 smoke 还在 load latency 捕获了 measured 不高于 baseline，并以 target exit 3 保留精确样本诊断。

结论：两个传输后端和 fail-closed 质量门均得到 QEMU 代理验证；只有 bare-metal 确定性代理结果用于默认开发 Profile。Linux 噪声数据未被删样本、降阈值或标成有效。以上均不是 BOOM v3 性能证据。
