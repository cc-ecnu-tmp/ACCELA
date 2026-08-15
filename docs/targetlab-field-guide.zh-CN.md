# TargetLab 无 Agent 现场测量手册

## 0. 合规停点

运行前由现场人员确认组委会允许执行通用硬件微基准。若不允许，立即停止 TargetLab，使用赛前合法且已验证的 Profile。禁止读取、探测或按官方隐藏测试程序与环境进行特化。

TargetLab 不经过 ACCELA 编译，只用显式指定的 RISC-V GCC/binutils 构建汇编微核。现场依赖仅为 POSIX sh、Python 3、make、目标 GCC/binutils；bare-metal 另需 OpenOCD 与 GDB。套件不联网、不使用容器、不安装运行时包。

解包后先运行 `python -m tools.targetlab selftest`。该命令在临时目录验证 `collect -> profile -> validate -> embed -> report` 全链路，不产生可用于比赛的校准 Profile；失败时不得进入现场测量。

## 1. Linux 后端

目标执行命令必须显式配置，例如现场提供的 SSH 包装脚本或板端命令：

```sh
python -m tools.targetlab configure \
  --backend linux \
  --cc riscv64-linux-gnu-gcc \
  --objcopy riscv64-linux-gnu-objcopy \
  --nm riscv64-linux-gnu-nm \
  --clock-hz 50000000 \
  --execute './field/run-on-target.sh' \
  --output targetlab-config.json
python -m tools.targetlab doctor targetlab-config.json
python -m tools.targetlab build targetlab-config.json
python -m tools.targetlab run targetlab-config.json --output raw.jsonl
python -m tools.targetlab collect raw.jsonl collected.json
```

程序先用 SIGILL 防护实测 `rdcycle`；可用时记录计时来源。不可访问时打印并记录显式 `clock_gettime` 来源。任何未声明的计时切换都是错误。

## 2. Bare-metal 与 Mailbox

`managed` 模式由 TargetLab 使用显式配置启动 OpenOCD 或 QEMU debug server、等待 GDB 端口，并在结束后只关闭自己启动的进程；服务日志始终保留。若现场已人工启动服务，使用 `external` 模式，TargetLab 不管理该进程。启动文件和 linker script 必须由板级环境提供：

```sh
python -m tools.targetlab configure \
  --backend baremetal \
  --cc riscv64-unknown-elf-gcc \
  --objcopy riscv64-unknown-elf-objcopy \
  --nm riscv64-unknown-elf-nm \
  --clock-hz 50000000 \
  --gdb riscv64-unknown-elf-gdb \
  --gdb-remote localhost:3333 \
  --debug-server-kind openocd \
  --debug-server-mode managed \
  --debug-server-executable openocd \
  --openocd-config field/boom.cfg \
  --startup field/start.S \
  --linker field/link.ld \
  --output targetlab-config.json
python -m tools.targetlab doctor targetlab-config.json
python -m tools.targetlab build targetlab-config.json
python -m tools.targetlab run targetlab-config.json --output raw.jsonl
python -m tools.targetlab collect raw.jsonl collected.json
```

固件把结果写入链接符号 `targetlab_mailbox`。Mailbox 包含 magic、版本、整体长度、状态、计数器能力位、条目数量和每项的 metric/category/source、样本数量及全部样本。主机由 GDB 符号解析起止位置，禁止硬编码地址；不依赖串口或 semihosting。magic、版本、长度、状态、条目范围或样本数任一异常都会保留 dump 并失败。

## 3. 采样和质量控制

每项 warmup 2 次、正式样本 9 次。先指数放大 kernel iterations，直到一次测量至少约一百万 cycle；每项使用配套 baseline 并逐样本扣除。JSONL 与 Mailbox 同时保留 iterations、normalization、baseline、measured 和扣除后值。延迟链每轮展开 128 次，吞吐微核按两条独立流共 256 次操作展开，pairing 每轮展开 128 对，再按实际操作或指令对数量归一化。保留全部样本，不裁剪离群值。套件覆盖依赖链、独立流、完整对称 pair matrix、可预测/伪随机分支、load-use、LSU、pointer chase、4/32/256 KiB working set、8/64/512 byte stride、call/ret、全局地址、64/256/1024 byte 前端尺寸、8/16/24/32 live-value 压力以及整数和浮点类别；SIMD 扩展点保持禁用。

计数器倒退、扣除结果非正、样本不足或 MAD 门槛失败均停止 profile。不要调低门槛来“修复”噪声，应先检查频率调节、后台负载、中断、热状态、目标复位和计时权限，然后完整重测。

## 4. Profile、嵌入与离线提交

```sh
python -m tools.targetlab profile collected.json \
  config/target/boomv3-development.json target-profile.json
python -m tools.targetlab validate target-profile.json
python -m tools.targetlab embed target-profile.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
python -m tools.targetlab verify-embedded target-profile.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
python -m tools.targetlab report target-profile.json target-profile-report.md
./gradlew --offline clean check
sh scripts/build-compiler-offline.sh native
```

`boomv3-development.json` 是未测量的现场结构模板。默认嵌入的 `qemu-rv64gc-development.json` 是 `qemu_proxy` 证据，不得作为 BOOM 模板继续生成硬件 Profile；现场 Profile 应从未测量模板生成，并使用唯一 `--profile-id` 标识本次目标和测量批次。

构建脚本只使用 JDK 21；`native` 模式还要求赛前已安装 GraalVM `native-image`，缺少时立即失败，不会悄悄交付依赖 JVM 的替代物。随后运行公开 smoke，确认 stdout、退出码和固定 compiler 接口未改变。诊断时才加 `--cost-trace decision-trace.jsonl`；不得把 trace 混入赛事 stdout。

## 5. QEMU 双后端实现验证

`sh scripts/validate-targetlab-qemu.sh` 使用同一套注册表分别执行 Linux user-mode JSONL 和 bare-metal 符号 Mailbox。默认使用 `riscv64-linux-gnu-*`、`riscv64-elf-*`、`qemu-riscv64`、`qemu-system-riscv64` 和支持 RISC-V 的 `gdb`；可用脚本中的 `TARGETLAB_*` 环境变量显式覆盖。`qemu_proxy` 会用可观测整数 ALU 代理 `mv`，避免 TCG 删除冗余 move 后产生虚假零耗时；除此之外仍保留完整度、原始样本和质量门槛。

QEMU system 的 `-icount` 可提供确定性代理周期，适合验证 Mailbox、校验、生成、嵌入和调度器启用链。QEMU user 的 `rdcycle` 可能受主机调度影响；若 MAD 超限，TargetLab 必须拒绝生成 Profile。该拒绝证明质量门正常工作，不应通过删样本或放宽阈值绕过。两种结果均只属于 QEMU 诊断证据。

## 6. 现场检查清单

1. 人工合规确认；工具链前缀、版本、ABI 和 code model 确认。
2. 计数器能力与显式计时来源确认；噪声预检通过。
3. 完整测量、原始 JSONL 和 Mailbox dump 留存。
4. 严格 validate、确定性 embed、verify-embedded 全通过。
5. 离线 clean build、功能 smoke、输出与退出码检查。
6. 仅在诊断副本启用 trace，核对合法性、成本分量、DryRunRA 和预算。
7. 任一步失败都保留原始诊断并回到上一个已验证发布；禁止自动改用旧模型或隐藏失败。

## 7. 故障定位

- `SIGILL` 后使用 clock：确认 S/U 模式计数器授权；这是显式低精度路径，不得改写为 rdcycle 数据。
- MAD 超限：先固定频率、隔离板端负载并冷却，重新执行完整九次采样；不删除样本。
- baseline 非正或计数器倒退：停止使用该计时源，保存原始输出和固件版本。
- Mailbox 长度或 magic 错误：核对 ELF 与正在运行固件一致、链接符号唯一、GDB target 未复位。
- embed 不一致：重新从同一 JSON 生成，禁止手改 Java。
- 编译器 verifier 或候选异常：保留 DecisionTrace 和输入的非敏感复现，不启用旧启发式兜底。
