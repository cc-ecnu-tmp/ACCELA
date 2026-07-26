# QEMU 性能分析工具

本目录包含用于测量 ACCELA 生成汇编的 RISC-V 裸机运行时和 QEMU TCG
插件。统一通过 `scripts/qemu-run.sh` 使用：脚本会编译 SysY 测试点、链接
适用于 `virt` 机器的 ELF、运行并校验输出，还可按需输出性能数据。

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

Default is for macOS.

| 变量                  | 默认值                         | 用途                                  |
|-----------------------|--------------------------------|---------------------------------------|
| `QEMU_PROFILE`        | `0`                            | 可选 `0`、`1`、`hotblocks` 或 `cache` |
| `QEMU_TIMEOUT`        | `120`                          | 单个测试点的超时秒数                  |
| `ACCELA_JAVA_HOME`    | `/opt/homebrew/opt/openjdk@21` | runner 使用的 JDK                     |
| `QEMU_PLUGIN_INCLUDE` | `/opt/homebrew/include`        | `qemu-plugin.h` 所在目录              |

## 测量边界与限制

裸机启动代码用保留标记包住 `main`，所以没有显式计时调用时，plugin 统计
完整的 `main`，不包含启动和退出过程。若程序调用 `_sysy_starttime` 和
`_sysy_stoptime`，对应的显式计时区间优先；指令计数器会累加所有已完成的
显式区间，热点块和 cache 报告对应最近一次区间。

cache plugin 模拟容量为 32 KiB、8 路组相联、cache line 为 64 字节并采用
LRU 替换的 L1D，不模拟延迟、预取、更高层缓存或 DRAM。TCG 指令数和 cache
估算适合用于比较优化前后变化。
