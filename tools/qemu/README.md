# QEMU 性能分析工具

本目录包含用于测量 ACCELA 生成汇编的 RISC-V 裸机运行时和 QEMU TCG
插件。统一通过 `scripts/qemu-run.sh` 使用：脚本会编译 SysY 测试点、链接
适用于 `virt` 机器的 ELF、运行并校验输出，还可按需输出性能数据。

裸机运行时实现了 SysY 的整数和单精度浮点 I/O，包括十进制/十六进制浮点
输入以及与标准运行时 `%a` 一致的十六进制输出。启动代码会启用 RV64GC
浮点状态，因此浮点运算、比较、类型转换和硬浮点 ABI 均可直接测试。

## 依赖

- JDK 21，以及已构建的 ACCELA 编译器
- 支持 RV64GC 的 `riscv64-elf-gcc`，以及用于正式 ELF 合同校验的
  `riscv64-elf-readelf`
- 启用了 plugin 支持的 `qemu-system-riscv64`
- C 编译器、`pkg-config`、GLib 2 和 QEMU 的 `qemu-plugin.h`, `timeout`
- 运行 GCC 13.3／Clang 18 对照时需要 Docker；WSL 可使用启用集成的
  `docker`，也可在 native daemon transport 不可达时使用 Windows `docker.exe`。
  首个 reachable daemon 的镜像检查失败不会跨 daemon 回退；reference launcher
  固定使用与快照版本完全一致的 `python3 -I`

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

使用 GCC `-O2` 或 Clang `-O3`、相同 RV64GC ABI 和运行时作为本地对照：

```sh
QEMU_COMPILER=gcc scripts/qemu-run.sh path/to/NAME.sy
QEMU_COMPILER=clang scripts/qemu-run.sh path/to/NAME.sy
```

统计动态指令和访存次数：

```sh
QEMU_PROFILE=1 scripts/qemu-run.sh path/to/NAME.sy
```

输出动态指令数最多的 20 个翻译块：

```sh
QEMU_PROFILE=hotblocks scripts/qemu-run.sh path/to/NAME.sy
# hotblock_rank=1 address=0x800003bc address_decimal=2147484604 executions=203012 instructions=6 dynamic=1218072
```

`hotblock_rank=1` 是唯一用于规范化热点指标的记录；显式 rank 避免多行 Top 20
输出被误解析为最热块。`address_decimal` 与十六进制地址表示同一无符号值，供
`run-record.v1` 记录规范化数值；当前 medany ELF 的地址范围可被精确表示。

估算 L1 数据缓存行为：

```sh
QEMU_PROFILE=cache scripts/qemu-run.sh path/to/NAME.sy
# l1d=32KiB/8-way/64B ... misses=... miss_rate=...
```

一次运行同时采集动态指令、load/store 和 L1D 模型计数：

```sh
QEMU_PROFILE=metrics scripts/qemu-run.sh path/to/NAME.sy
```

可以用下面的命令将热点地址映射回生成的代码：

```sh
riscv64-elf-objdump -d build/qemu-run/NAME/program.elf
```

生成的汇编、ELF、程序输出、plugin 和日志都保存在 `build/qemu-run/NAME/` 下。

## 环境变量

所有默认值都通过 `PATH` 或标准 include 目录发现，不包含本机绝对路径。

| 变量                  | 默认值                    | 用途                                                    |
|-----------------------|---------------------------|---------------------------------------------------------|
| `QEMU_PROFILE`        | `0`                       | `0`、`instructions`、`hotblocks`、`cache` 或 `metrics`  |
| `QEMU_COMPILER`       | `accela`                  | `accela`、`benchmark`、`gcc` 或 `clang`                  |
| `QEMU_TIMEOUT`        | `120`                     | 单个测试点的超时秒数                                    |
| `RISCV_GCC`           | `riscv64-elf-gcc`         | 仅 `qemu-run.sh` 的交互式覆盖；正式 `benchmark-link.sh` 拒绝该变量 |
| `ACCELA_JAVA_HOME`    | `JAVA_HOME`，然后 `PATH`  | JDK 21；只填 JDK 根目录                                  |
| `QEMU_PLUGIN_INCLUDE` | 自动发现标准目录          | `qemu-plugin.h` 所在目录                                 |
| `ACCELA_PIPELINE_PROFILE` | 无                    | `QEMU_COMPILER=benchmark` 时必需的 profile JSON          |

## 测量边界与限制

裸机启动代码用保留标记包住 `main`，所以没有显式计时调用时，plugin 统计
完整的 `main`，不包含启动和退出过程。若程序调用 `_sysy_starttime` 和
`_sysy_stoptime`，对应的显式计时区间优先；指令计数器会累加所有已完成的
显式区间。指令和 cache 计数会累加所有已完成的显式区间；每个 cache
区间都从空的 32 KiB L1D 模型开始，避免启动代码或前一区间污染结果。
热点块报告仍对应最近一次完整区间。

计时标记必须严格配对且位于 `main` 内。重复开始、无开始的结束、未闭合区间、
重复或缺失的 `main` 边界都会只输出 `*_error=<reason>`，不会输出看似有效的
指标行；评测入口因此把该次执行判为工具失败，而不是继续参与排名。

三种 profile 都排除 SysY I/O 运行时函数本身；用户代码中的调用仍计数。
这避免 UART 轮询次数受主机调度影响，使同一 ELF 的重复测量保持一致。

GCC/Clang 对照固定为 RV64GC、LP64D、medany，并显式使用整数 wrap、关闭
fast-math 与浮点融合。它们和本目录的裸机 runtime 只构成本地代理基线；在
官方决赛 runtime、完整链接命令与 BOOM 平台结果公布前，不得写成官方成绩。

cache plugin 模拟容量为 32 KiB、8 路组相联、cache line 为 64 字节并采用
LRU 替换和 write-allocate。每次访存按起始地址访问一条 line；模型不展开
跨 line 的访问，也不模拟延迟、预取、更高层缓存或 DRAM。SysY 标量访问通常
自然对齐，但分析非常规或手写宽访存时必须注明这一限制。TCG 指令数和 cache
估算适合用于比较优化前后变化。
