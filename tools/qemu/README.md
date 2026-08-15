# QEMU 性能分析工具

本目录包含用于测量 ACCELA 生成汇编的 RISC-V 裸机运行时和 QEMU TCG
插件。统一通过 `scripts/qemu-run.sh` 使用：脚本会编译 SysY 测试点、链接
适用于 `virt` 机器的 ELF、运行并校验输出，还可按需输出性能数据。

裸机运行时实现了 SysY 的整数和单精度浮点 I/O，包括十进制/十六进制浮点
输入以及与标准运行时 `%a` 一致的十六进制输出。启动代码会启用 RV64GC
浮点状态，因此浮点运算、比较、类型转换和硬浮点 ABI 均可直接测试。

## 依赖

- JDK 21，以及已构建的 ACCELA 编译器
- 支持 RV64GC 的 `riscv64-elf-gcc`
- 启用了 plugin 支持的 `qemu-system-riscv64`
- C 编译器、`pkg-config`、GLib 2 和 QEMU 的 `qemu-plugin.h`, `timeout`

第一次运行前先构建 ACCELA：

```sh
JAVA_HOME=/path/to/jdk21/ bash gradlew classes --no-daemon
```

每个测试点必须包含位于同一目录的 `NAME.sy` 和 `NAME.out`；没有输入时可省略
`NAME.in`。

## 运行测试点

只做正确性检查：

```sh
scripts/qemu-run.sh path/to/NAME.sy
```

使用 LLVM `-O3`、相同 RV64GC ABI 和运行时作为对照：

```sh
QEMU_COMPILER=llvm scripts/qemu-run.sh path/to/NAME.sy
```

统计动态指令和访存次数：

```sh
QEMU_PROFILE=1 scripts/qemu-run.sh path/to/NAME.sy
# instructions=3528844 loads=0 stores=0
```

输出动态指令数最多的 20 个翻译块：

```sh
QEMU_PROFILE=hotblocks scripts/qemu-run.sh path/to/NAME.sy
# 0x800003bc executions=203012 instructions=6 dynamic=1218072
```

估算 L1 数据缓存行为：

```sh
QEMU_PROFILE=cache scripts/qemu-run.sh path/to/NAME.sy
# l1d=32KiB/8-way/64B ... misses=... miss_rate=...
```

可以用下面的命令将热点地址映射回生成的代码：

```sh
riscv64-elf-objdump -d build/qemu-run/NAME/program.elf
```

生成的汇编、ELF、程序输出、plugin 和日志都保存在 `build/qemu-run/NAME/` 下。

## 环境变量

脚本同时支持 macOS 的 `.dylib` 和 Linux/WSL 的 `.so` QEMU 插件构建，并根据
宿主系统选择正确的链接参数。QEMU plugin API 兼容旧版回调 ABI 和
`QEMU_PLUGIN_VERSION >= 7` 的新回调 ABI。

| 变量                  | 默认值                         | 用途                                  |
|-----------------------|--------------------------------|---------------------------------------|
| `QEMU_PROFILE`        | `0`                            | 可选 `0`、`1`、`hotblocks` 或 `cache` |
| `QEMU_COMPILER`       | `accela`                       | 可选 `accela` 或 `llvm`               |
| `QEMU_TIMEOUT`        | `120`                          | 单个测试点的超时秒数                  |
| `LLVM_CLANG`          | `clang`                        | LLVM 对照编译器路径                    |
| `ACCELA_JAVA`         | `JAVA_HOME/bin/java` 或 `java` | runner 使用的 Java 21 命令             |
| `ACCELA_CLASSES`      | `build/classes/java/main`      | 显式选择待测 ACCELA class 目录         |
| `QEMU_WORK_ROOT`      | `build/qemu-run`               | 隔离不同编译器/实验的运行产物          |

两个 ACCELA class 目录的冷启动配对比较使用：

```sh
ACCELA_JAVA=java \
  scripts/qemu-compare-classes.sh BASELINE_CLASSES CANDIDATE_CLASSES \
  testsuite/functional/NAME.sy
```

默认执行五组配对，每组都会重新启动两个 compiler、重新链接、校验输出并用 QEMU plugin 记录动态指令数。输出列依次为 case、run、baseline instructions、candidate instructions、`baseline/candidate`、双方 compiler 秒数、双方峰值字节数和双方编译器对象 `.text` 字节数。compiler 测量由独立 Python 进程使用 `wait4` 获取，不复用编译缓存。可分别用 `RISCV_GCC`、`RISCV_SIZE` 显式指定交叉工具。该结果属于 `qemu_proxy`，不能替代 BOOM 周期证据。

批量比较时，`CASE_LIST` 每行写一个不带 `.sy` 的 case ID，并使用全新的输出目录：

```sh
QEMU_PAIRED_JOBS=4 \
  scripts/qemu-compare-corpus.sh BASELINE_CLASSES CANDIDATE_CLASSES \
  testsuite/functional CASE_LIST OUTPUT
```

`OUTPUT/results.tsv` 只会在全部 case 完成后生成；任一编译、链接、输出差分、QEMU 或计数解析失败都会让命令失败并保留各 case 的诊断边界。
| `QEMU_PLUGIN_INCLUDE` | Linux 使用编译器默认搜索路径；Darwin 必填 | `qemu-plugin.h` 目录 |

## 测量边界与限制

裸机启动代码用保留标记包住 `main`，所以没有显式计时调用时，plugin 统计
完整的 `main`，不包含启动和退出过程。若程序调用 `_sysy_starttime` 和
`_sysy_stoptime`，对应的显式计时区间优先；指令计数器会累加所有已完成的
显式区间，热点块和 cache 报告对应最近一次区间。

三种 profile 都排除 SysY I/O 运行时函数本身；用户代码中的调用仍计数。
这避免 UART 轮询次数受主机调度影响，使同一 ELF 的重复测量保持一致。

cache plugin 模拟容量为 32 KiB、8 路组相联、cache line 为 64 字节并采用
LRU 替换的 L1D，不模拟延迟、预取、更高层缓存或 DRAM。TCG 指令数和 cache
估算适合用于比较优化前后变化。
