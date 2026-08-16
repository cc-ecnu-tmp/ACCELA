# BOOM v3 板端 TargetLab 取样操作手册

本文给出在 BOOM v3 实板上生成 ACCELA `target-profile.v1` 的完整现场流程。目标是得到可追溯、可重复、能够驱动 R1/R2 成本调度器的硬件 Profile，而不是探测赛事用例。所有命令都从 ACCELA 仓库根目录执行，路径均为仓库相对路径。

统一流程为：

```text
人工合规确认 -> 板卡身份冻结 -> 工具自检 -> 噪声预检
-> configure -> doctor -> build -> run -> collect
-> profile -> validate -> embed -> verify-embedded -> report
-> 离线构建与 smoke -> 配对性能测量 -> 归档
```

任何一步失败都停止当前批次并保留原始诊断。不得删除离群样本、降低稳定性门槛、补写缺失指标或自动改用开发 Profile。

## 1. 适用范围和证据边界

本手册适用于 RV64GC、LP64D、`medany`、双发射 BOOM v3。实际板卡必须逐项核对，不能只根据板卡名称假定 ISA、频率、缓存、核数或计数器权限。

开始前由现场负责人书面或现场确认：

1. 允许运行与官方测试程序无关的通用汇编微基准。
2. 允许读取 `cycle`/`instret` 或使用单调时钟。
3. 允许在指定 hart 上固定亲和性、调整频率策略或复位目标。
4. 允许保存 Profile、原始样本和不含隐藏用例信息的环境记录。

任一项不允许时停止 TargetLab，使用赛前已经合法生成并验证的 Profile。不得读取、枚举、计时或推断隐藏测试程序，也不得把文件名、源码、case ID、输出或测试指纹带入 Profile。

只有真实 BOOM v3 产生且完整通过质量门的数据可以记录为 `target_hardware`。QEMU、其他 RISC-V 核和声明模板都不能提升为 BOOM 证据。硬件 Profile 只能证明成本参数来自该目标；“超越 LLVM”还必须完成同板、同 ABI、同运行时、同输入的 ACCELA/组委会 LLVM 配对测量。

## 2. 选择测量后端

优先按以下顺序选择：

| 条件 | 后端 | 计时 | 推荐程度 |
|---|---|---|---|
| BOOM 板可稳定运行 RV64 Linux | Linux，工具与套件均在板端本地运行 | 优先 `rdcycle`，不可用时显式 `clock_gettime` | 首选 |
| BOOM 板只有调试接口，且已有经过验证的启动文件、linker script 和 OpenOCD 配置 | Bare-metal Mailbox | 必须可用 `rdcycle` | 可用 |
| 只有 QEMU 或非 BOOM RISC-V 板 | 不生成 BOOM Profile | 代理计时 | 仅诊断 |

不要为了使用 bare-metal 而临时猜测复位地址、RAM 范围、栈顶、hart 选择或时钟。此类错误可能产生看似完整但属于错误目标的数据。Linux 后端能够运行时，板端本地 Linux 路径更容易审计，也避免远程复制和 shell 包装器污染 stdout。

## 3. 准备离线材料

现场包至少包含：

- 当前 R2 源码及提交标识；
- Python 3、make 和 JDK 21；
- Linux 路径所需的本机 GCC/binutils，或 bare-metal 路径所需的 `riscv64-unknown-elf-*`、RISC-V GDB 与 OpenOCD；
- Gradle 离线缓存以及构建最终 `compiler` 所需的本地依赖；
- `config/target/boomv3-development.json` 未校准模板；
- 公开功能 smoke、公开性能语料及其输入/期望输出；
- bare-metal 路径额外需要由板卡维护者提供的启动文件、linker script 和 OpenOCD 配置。

现场禁止联网安装包。解包后先确认工作树和工具版本：

```sh
git status --short
git rev-parse HEAD
python3 --version
make --version
java -version
```

工作树必须与计划嵌入 Profile 的提交对应。若必须记录现场修改，应先单独提交并重新记录提交标识，不能用无法重建的脏工作树生成正式归档。

## 4. 建立本次证据目录

每次完整尝试使用新的目录。失败批次不得覆盖或删除：

```sh
run_id=boomv3-$(date -u +%Y%m%dT%H%M%SZ)
evidence_dir=measurements/$run_id
mkdir -p "$evidence_dir" "$evidence_dir/environment" "$evidence_dir/profile"
```

建议保存以下内容：

```text
measurements/<run-id>/
  environment/
    operator-note.md
    tool-versions.txt
    cpuinfo.txt
    kernel.txt
    frequency.txt
    interrupts-before.txt
    interrupts-after.txt
  targetlab-config.json
  raw.jsonl
  raw.jsonl.mailbox.bin       # bare-metal 才有
  raw.jsonl.openocd.log       # managed OpenOCD 才有
  collected.json
  profile/
    target-profile.json
    target-profile-report.md
  comparison/
    paired.tsv
    results.json
    report.md
  README.md
```

`operator-note.md` 至少记录板卡资产标识、BOOM/Chipyard 配置或 bitstream 标识、测量 hart、供电和散热状态、Linux 或固件标识、合规确认人、开始/结束 UTC 时间、失败和重试原因。不要写入密码、私钥、隐藏用例名称或本机绝对路径。

## 5. 冻结 BOOM 板卡身份和频率

### 5.1 Linux 身份采集

在板端运行：

```sh
uname -a > "$evidence_dir/environment/kernel.txt"
cat /proc/cpuinfo > "$evidence_dir/environment/cpuinfo.txt"
cat /proc/interrupts > "$evidence_dir/environment/interrupts-before.txt"
{
  command -v gcc && gcc --version
  command -v objcopy && objcopy --version
  command -v nm && nm --version
  command -v taskset && taskset --version
} > "$evidence_dir/environment/tool-versions.txt" 2>&1
```

如果系统提供 CPU frequency 接口，再保存当前策略：

```sh
for path in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor \
            /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do
  if [ -r "$path" ]; then
    printf '%s=' "$path"
    cat "$path"
  fi
done > "$evidence_dir/environment/frequency.txt"
```

FPGA 上的 BOOM 常常没有 cpufreq 接口。接口缺失时在记录中写明“固定 FPGA 时钟/无 cpufreq 接口”，不要伪造 governor 值。

### 5.2 确定 `--clock-hz`

`--clock-hz` 必须是本次 BOOM 核 `cycle` 计数所对应的核心频率。来源优先级为：

1. 本次 FPGA bitstream/SoC 构建清单中的 BOOM 核时钟；
2. 经过板卡维护者确认的时钟树或 Linux cpufreq 固定值；
3. 在固定频率状态下，用 `rdcycle` 与 `CLOCK_MONOTONIC` 长窗口比对得到的交叉检查结果。

设备树中的 RISC-V `timebase-frequency` 通常描述 `time` 计数器，不等于 `cycle` 频率，不能直接作为 `--clock-hz`。如果无法确定核心频率，停止正式取样并向板卡维护者确认。

Linux 最终使用 `clock_gettime` 时，TargetLab 需要该频率把纳秒换算为周期，因此错误频率会直接污染整个 Profile。使用 `rdcycle` 时，频率仍会写入目标身份和最小运行时控制，也必须准确。

### 5.3 选择并隔离 hart

选择不承担主要中断和系统服务的 BOOM hart，例如把 `boom_core` 设置为现场确认的逻辑 CPU 编号：

```sh
boom_core=3
taskset -c "$boom_core" true
```

测量前：

- 停止非必要的构建、日志轮转、定时任务和网络服务；
- 保留远程管理和安全必需服务，不要为了降噪破坏板卡可恢复性；
- 若系统支持且现场允许，将所选核心固定到稳定频率，并记录原 governor，结束后恢复；
- 预热到稳定温度，避免第一次运行处于冷启动频率或缓存状态；
- 不要与编译、文件同步或其他 benchmark 并行。

## 6. 运行仓库自检

正式取样前执行：

```sh
python3 -m tools.targetlab selftest
python3 -m tools.targetlab validate config/target/boomv3-development.json
```

自检验证严格 JSON、注册表、统计、生成、嵌入和报告链路，但不产生 BOOM 校准数据。模板必须保持 `calibrated:false`、`evidence_level:"declared"`。任何失败都先修复工具或离线依赖，不能跳过，也不能用 QEMU Profile 或其他板卡 Profile 作为现场模板。

## 7. Linux 后端：推荐路径

以下命令假定整个现场包已离线复制到 BOOM Linux 板，并从仓库根目录运行。原生工具链名称按板端实际安装填写；不要让 TargetLab 自行搜索默认工具。

### 7.1 配置

```sh
clock_hz=100000000
boom_core=3

python3 -m tools.targetlab configure \
  --backend linux \
  --cc gcc \
  --objcopy objcopy \
  --nm nm \
  --clock-hz "$clock_hz" \
  --minimum-cycles 1000000 \
  --timeout-seconds 7200 \
  --measurement-mode hardware \
  --execute "taskset -c $boom_core" \
  --build-dir "$evidence_dir/build-linux" \
  --output "$evidence_dir/targetlab-config.json"
```

示例中的 `100000000` 和 hart `3` 只是占位值，必须替换为本次板卡确认值。若板端使用显式交叉前缀，则将三个工具改为例如 `riscv64-linux-gnu-gcc`、`riscv64-linux-gnu-objcopy` 和 `riscv64-linux-gnu-nm`。

### 7.2 能力检查、构建和噪声预检

```sh
python3 -m tools.targetlab doctor "$evidence_dir/targetlab-config.json"
python3 -m tools.targetlab build "$evidence_dir/targetlab-config.json"
```

`doctor` 必须确认工具存在、版本可执行、配置路径有效。`build` 还会检查 ELF 中三个前端微核的精确尺寸以及 `targetlab_mailbox`、`targetlab_done` 符号；符号或尺寸不符时不能继续。

正式完整取样前，确认所选 hart 无明显持续中断增长、板卡温度和频率稳定。TargetLab 不提供“少测几个 metric”的正式模式；预检通过后一次执行完整注册表，以免部分数据被误当作 Profile。

### 7.3 完整取样

```sh
python3 -m tools.targetlab run \
  "$evidence_dir/targetlab-config.json" \
  --output "$evidence_dir/raw.jsonl"
```

Linux 程序会在 SIGILL 防护下实际探测 `rdcycle` 和 `rdinstret`：

- `rdcycle` 可用：环境行记录 `timer:"rdcycle"`；
- `rdcycle` 不可用：明确记录 `timer:"clock_gettime"`，不会静默伪装成 cycle；
- `rdinstret` 不可用：记录 `false`，但不阻止基于周期或单调时钟的 Profile；
- 计数器倒退、baseline 扣除非正或目标异常：进程非零退出并保留已写出的诊断。

不要重定向额外日志到 `raw.jsonl`。该文件必须只包含 TargetLab JSONL；板端命令、SSH banner、`taskset` 诊断和 shell 调试输出应写 stderr 或单独日志。

### 7.4 收集并检查完整性

```sh
python3 -m tools.targetlab collect \
  "$evidence_dir/raw.jsonl" \
  "$evidence_dir/collected.json"
```

成功归档应包含一条环境记录和完整的 121 项注册指标。每项必须具有 2 次 warmup 后的 9 个正式样本、正的 `iterations`/`normalization`、9 个 baseline、9 个 measured、9 个逐样本扣除结果，以及与环境计时源一致的单位。不要仅凭行数手工宣布成功；`collect` 和后续 `profile` 会检查重复、缺失、额外指标及样本结构。

## 8. Bare-metal/OpenOCD 后端

只有板级资料完整并经过单独启动验证时使用本路径。

### 8.1 板级文件必须满足的契约

启动文件必须选择正确 hart，建立 `gp` 和对齐的 `sp`，清零 `.bss`，按加载方式初始化 `.data`，为 RV64GC 浮点微核启用合法 FPU 状态，并最终进入 `targetlab_done`。linker script 必须使用本板真实可写 RAM、保留 `targetlab_mailbox` 完整对象、定义栈顶和 section 符号，并与 OpenOCD 的 `load` 地址视图一致。OpenOCD 配置必须明确 adapter、transport、JTAG 链和目标 hart。不要复制其他 BOOM/FPGA 板的配置后只改文件名。

### 8.2 配置与运行

```sh
clock_hz=100000000

python3 -m tools.targetlab configure \
  --backend baremetal \
  --cc riscv64-unknown-elf-gcc \
  --objcopy riscv64-unknown-elf-objcopy \
  --nm riscv64-unknown-elf-nm \
  --clock-hz "$clock_hz" \
  --minimum-cycles 1000000 \
  --timeout-seconds 7200 \
  --measurement-mode hardware \
  --gdb riscv64-unknown-elf-gdb \
  --gdb-remote localhost:3333 \
  --debug-server-kind openocd \
  --debug-server-mode managed \
  --debug-server-executable openocd \
  --openocd-config field/boomv3.cfg \
  --startup field/boomv3-start.S \
  --linker field/boomv3-link.ld \
  --build-dir "$evidence_dir/build-baremetal" \
  --output "$evidence_dir/targetlab-config.json"

python3 -m tools.targetlab doctor "$evidence_dir/targetlab-config.json"
python3 -m tools.targetlab build "$evidence_dir/targetlab-config.json"
python3 -m tools.targetlab run \
  "$evidence_dir/targetlab-config.json" \
  --output "$evidence_dir/raw.jsonl"
python3 -m tools.targetlab collect \
  "$evidence_dir/raw.jsonl" \
  "$evidence_dir/collected.json"
```

示例频率同样只是占位值。`managed` 模式由 TargetLab 启动 OpenOCD、等待 GDB 端口并只终止自己启动的进程；日志保存在 `raw.jsonl.openocd.log`。若 OpenOCD 已由现场人员管理，改用 `--debug-server-mode external`。

GDB 使用 ELF 符号解析 `targetlab_mailbox`，不使用硬编码地址。Mailbox dump 保存为 `raw.jsonl.mailbox.bin`。magic、版本、长度、状态、计数器能力、样本数、保留位或失败字段任一不符都会拒绝解码。Bare-metal 成功路径必须证明 `rdcycle` 可用；不存在 `clock_gettime` 降级。

## 9. 生成并验证 BOOM Profile

Linux 或 bare-metal 的 `collect` 成功后，使用未测量模板生成唯一 Profile：

```sh
profile_id="$run_id-rv64gc-lp64d"

python3 -m tools.targetlab profile \
  "$evidence_dir/collected.json" \
  config/target/boomv3-development.json \
  "$evidence_dir/profile/target-profile.json" \
  --profile-id "$profile_id"

python3 -m tools.targetlab validate \
  "$evidence_dir/profile/target-profile.json"

python3 -m tools.targetlab report \
  "$evidence_dir/profile/target-profile.json" \
  "$evidence_dir/profile/target-profile-report.md"
```

`profile` 会重新计算所有样本，检查完整 pairing 上三角并生成对称矩阵。质量门固定为：

- 算术、吞吐和 pairing：`MAD / median <= 1%`；
- branch、memory、working-set 和 spill 转折：`MAD / median <= 5%`；
- 每项恰好 9 个正的扣除后样本；
- 计数器不倒退，measured 必须大于对应 baseline；
- 不裁剪离群点，不从模板继承未测量占位值。

任一指标失败时，本次 `raw.jsonl`、`collected.json` 和日志归入 rejected 批次。先检查中断、频率、热状态、计时权限和目标复位，再用新的 `run_id` 完整重测。不得只重测失败项并拼接两个批次。

成功 Profile 应显示：

- `profile.calibrated: true`；
- `profile.evidence_level: "target_hardware"`；
- `measurement_environment.backend` 与实际后端一致；
- `measurement_environment.measurement_mode: "hardware"`；
- `measurement_environment.clock_hz` 与本次 BOOM 身份一致；
- `warmup_count: 2`、`sample_count: 9`；
- SIMD 在正式 ISA/ABI 未确认前仍为 `enabled:false`。

## 10. 嵌入并验证编译器

先在专用分支或候选提交中嵌入，不直接覆盖已验证发布：

```sh
python3 -m tools.targetlab embed \
  "$evidence_dir/profile/target-profile.json" \
  src/main/java/accela/cost/GeneratedTargetProfile.java

python3 -m tools.targetlab verify-embedded \
  "$evidence_dir/profile/target-profile.json" \
  src/main/java/accela/cost/GeneratedTargetProfile.java

./gradlew --offline clean check \
  -PtargetProfile="$evidence_dir/profile/target-profile.json"

sh scripts/build-compiler-offline.sh native
```

`verify-embedded` 必须逐字节通过。禁止手工编辑 `GeneratedTargetProfile.java`。最终 compiler 自包含，不在赛事运行时读取 JSON，也不提供环境变量或命令行 Profile 覆盖。

构建后用公开 smoke 验证固定接口、stdout 和退出码：

```sh
./compiler input.sy -S -o output.s -O1
```

调试副本可显式添加 `--cost-trace decision-trace.jsonl` 检查 Profile ID、成本分量、DryRunRA、剪枝和预算；正式调用不得启用 trace，也不得把 trace 写入 stdout。

## 11. 在 BOOM 上做配对无退化测量

Profile 取样通过不代表调度结果一定更快。至少对 R1/R2 做五次冷启动配对测量。两侧必须使用相同 BOOM 板、hart、频率、工具链、ABI、runtime、语料和输入。

在 BOOM Linux 板端准备 R1/R2 Java 类目录和公开语料后运行：

```sh
mkdir -p "$evidence_dir/comparison"

taskset -c "$boom_core" python3 tools/benchmark/run_linux_paired.py \
  --baseline-classes build/classes-r1 \
  --candidate-classes build/classes-r2 \
  --corpus testsuite/functional \
  --case-list measurements/cases.txt \
  --runtime testsuite/libsysy/sylib.c \
  --output "$evidence_dir/comparison/paired.tsv" \
  --runs 5 \
  --warmups 2 \
  --timeout-seconds 300 \
  --java java \
  --gcc gcc \
  --size size

python3 -m tools.benchmark import-linux \
  "$evidence_dir/comparison/paired.tsv" \
  "$evidence_dir/comparison/results.json" \
  --comparison r2_r1 \
  --target rv64gc \
  --abi lp64d \
  --runtime "$profile_id"

python3 -m tools.benchmark report \
  "$evidence_dir/comparison/results.json" \
  "$evidence_dir/comparison/report.md"
```

runner 对每次 run 冷启动两侧 compiler、检查汇编确定性、交替 AB/BA 顺序、执行反向预热、验证 stdout/退出码，并逐行 flush 到 `.partial`。中断或超时后保留部分结果，但 `.partial` 不能作为完整证据。

R2 对 R1 的无退化结果只能说明调度升级没有破坏现有 ACCELA。最终 R2 发布还必须使用组委会提供的 LLVM 基线执行独立的 `r2_llvm` 配对协议：速度比定义为 `LLVM 时间 / ACCELA 时间`，按 case 配对 bootstrap，GM 95% 置信下界必须大于 `1.00`，任一样例不得低于 `0.90`。在组委会 LLVM 的固定调用接口尚未明确前，不要编造包装命令或把 R1/R2 runner 的结果改名为 LLVM 结果。

## 12. 结束、恢复和归档

测量结束后，在 Linux 板上保存最终中断状态：

```sh
cat /proc/interrupts > "$evidence_dir/environment/interrupts-after.txt"
```

若修改过 governor、服务或中断亲和性，按 `operator-note.md` 中记录的原值逐项恢复。确认没有遗留 TargetLab、GDB、OpenOCD 或 benchmark 进程，再关闭或交还板卡。

归档 README 至少列出：

- ACCELA commit 和 Profile ID；
- BOOM/Chipyard/bitstream/板卡身份；
- backend、hart、频率来源、计时源及计数器能力；
- 工具链、内核或 bare-metal 固件标识；
- 121 项完整度及质量门结果；
- 所有 rejected 批次和原因；
- embed/verify/build/smoke 状态；
- R1/R2 和 R2/LLVM 配对覆盖率、GM、95% CI、最差样例；
- 仍未关闭的发布门。

原始 JSONL、Mailbox、OpenOCD 日志和失败状态是正式诊断的一部分。可对外提交的 Profile 不应包含密码、IP、用户名、私钥、本机绝对路径、隐藏用例或赛事环境秘密。

## 13. 常见失败与处理

| 现象 | 含义 | 处理 |
|---|---|---|
| `doctor` 找不到工具 | 离线包或配置不完整 | 补齐明确工具链，重新开始；不改用宿主工具 |
| Linux `rdcycle:false` | S/U 模式无计数器权限或触发 SIGILL | 核对固件授权；允许时接受明确的 `clock_gettime`，并保证 `clock_hz` 正确 |
| Bare-metal 无 `rdcycle` | Mailbox 证据不满足 bare-metal 契约 | 修复特权级/计数器授权；不能降级 |
| target exit 3 或 Mailbox status 3 | 计数器倒退或 measured 不高于 baseline | 保存失败 metric/sample/baseline/measured，检查复位、时钟和微核执行环境 |
| `MAD / median` 超限 | 当前板端噪声过高 | 固定频率、降低后台负载、稳定温度后完整重测；不删离群点 |
| 121 项不完整或重复 | 执行中断、输出污染或批次拼接 | 保留原始文件，用新 `run_id` 完整重跑 |
| Mailbox magic/length 错 | ELF、内存布局、符号或当前固件不一致 | 核对 GDB 连接、本次 ELF、linker 和复位流程 |
| GDB 到不了 `targetlab_done` | 启动、hart、FPU、异常或内存映射错误 | 查看 OpenOCD/GDB 日志，先单独验证板级启动契约 |
| `profile` 拒绝单位或样本 | 配置、环境行和原始数据不一致 | 修复根因并重测，不手改 JSON |
| `verify-embedded` 不一致 | 生成 Java 过期或被手改 | 从同一 Profile 重新执行 `embed` |
| BOOM wall-clock 波动但代码相同 | 调度、系统中断或温度噪声可能主导 | 增加诊断，不删除样本；按门槛诚实报告 CI 和最差值 |

## 14. 现场最终检查清单

- [ ] 已人工确认允许通用微基准，且未接触隐藏测试程序。
- [ ] ACCELA commit、工作树状态、BOOM/Chipyard/bitstream 身份已记录。
- [ ] RV64GC、LP64D、`medany`、issue width 和目标 hart 已核对。
- [ ] 核心频率来自可信来源，未把 `timebase-frequency` 当成核心频率。
- [ ] Python、make、JDK、GCC/binutils，以及可选 GDB/OpenOCD 均离线可用。
- [ ] `selftest`、模板 `validate`、`doctor`、`build` 全部通过。
- [ ] 正式批次包含一条环境记录和完整 121 项，每项 9 个原始样本。
- [ ] 所有 MAD、baseline、计数器和单位门禁通过，没有裁剪或拼接样本。
- [ ] Profile 为 `calibrated:true`、`target_hardware`、`hardware`，且目标身份正确。
- [ ] `embed`、`verify-embedded`、离线 clean build 和公开 smoke 全部通过。
- [ ] R1/R2 至少五次冷启动同机配对；LLVM 另按组委会基线协议测量。
- [ ] governor、服务和调试进程已恢复，失败批次及原始诊断均保留。
- [ ] 未通过 BOOM/LLVM 发布门时，报告明确写“候选”，不宣称超越 LLVM。
