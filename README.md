# ACCELA

ACCELA 是面向 RV64GC/LP64D 的 SysY 编译器。本分支加入目标校准成本调度器 R1、分层 R2 Beam 与离线 TargetLab 测量套件；编译器仍使用固定赛事接口：

```sh
./compiler input.sy -S -o output.s -O1
```

目标参数由经过严格校验的 `target-profile.v1` JSON 在构建时生成 Java 源码并嵌入最终编译器。赛事运行时不读取配置文件，不联网，也不接收测试用例标识。默认内置 Profile 来自 bare-metal QEMU 的确定性代理测量，标记为 `evidence_level: qemu_proxy`；它用于实际驱动和验证成本调度器，但不代表 BOOM v3 硬件校准，也不能作为超越 LLVM 的发布证据。

仓库同时保存一份 [VisionFive 2 U74 Linux 实测归档](measurements/visionfive2-u74-linux-20260816/README.md)，用于演示无 Agent 现场校准、失败批次保留和 R1/R2 目标硬件诊断。它不是默认比赛 Profile，也不提升为 BOOM 证据。

## 开发验证

```sh
python -m tools.targetlab validate config/target/qemu-rv64gc-development.json
python -m tools.targetlab selftest
python -m tools.targetlab verify-embedded \
  config/target/qemu-rv64gc-development.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
./gradlew check
```

在安装了 RISC-V GNU 工具链、QEMU user/system 和支持 RISC-V 的 GDB 的 POSIX 主机上，可执行 `sh scripts/validate-targetlab-qemu.sh`。脚本分别验证 Linux JSONL 传输链和 bare-metal 符号 Mailbox 链；Linux QEMU 计时若超过正式 MAD 门槛会明确拒绝 Profile，脚本保留原始证据并报告该质量拒绝。

现场测量从人工合规确认开始，统一流程为 `configure -> build -> run -> collect -> profile -> validate -> embed -> report`。详见：

- [成本调度器架构](docs/cost-scheduler-architecture.zh-CN.md)
- [R2 分层调度器状态与边界](docs/r2-scheduler-status.zh-CN.md)
- [TargetProfile JSON v1 规范](docs/target-profile-json-v1.zh-CN.md)
- [TargetLab 现场手册](docs/targetlab-field-guide.zh-CN.md)
- [BOOM v3 板端取样操作手册](docs/targetlab-boomv3-sampling.zh-CN.md)
- [Linux 测量后端](docs/targetlab-linux.zh-CN.md)
- [Bare-metal、OpenOCD 与 Mailbox](docs/targetlab-baremetal.zh-CN.md)
- [Profile 生成与嵌入](docs/profile-embedding.zh-CN.md)
- [DecisionTrace 解读](docs/decision-trace.zh-CN.md)
- [故障诊断](docs/troubleshooting.zh-CN.md)
- [比赛合规边界](docs/competition-compliance.zh-CN.md)
- [LLVM 对标与发布门禁](docs/benchmark-and-release.zh-CN.md)
- [QEMU 开发 Profile 示例报告](docs/examples/qemu-rv64gc-development-report.md)
- [QEMU 双后端验证记录](docs/examples/qemu-dual-backend-validation.md)
- [BOOM v3 现场模板示例报告](docs/examples/boomv3-development-report.md)
