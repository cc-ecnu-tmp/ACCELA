# TargetLab Bare-metal、OpenOCD 与 Mailbox

Bare-metal 后端不依赖串口、semihosting或固定 RAM 地址。固件在链接符号 `targetlab_mailbox` 中写入版本化结果，GDB 通过符号表达式导出整个对象。板级启动文件负责建立 GP/SP、清零 BSS、启用 RV64GC 浮点状态；linker script 负责 RAM 布局和栈顶。

## OpenOCD 现场模式

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
  --output targetlab-baremetal.json
```

`managed` 会启动并只终止本次启动的服务，日志写在 raw 文件旁。`external` 只连接人工启动的 server。OpenOCD 路径执行 `reset halt` 和 `load`；QEMU 路径由 `-kernel` 加载 ELF，不能混用。

## Mailbox ABI v1

Header 固定 96 字节，包含 magic、version、status、total length、entry count、计数器 flags、clock、最小周期、warmup/sample 数、measurement mode 和失败样本诊断。每项固定 344 字节，包含 48 字节 metric、32 字节 category、24 字节 source、iterations、normalization，以及三组九个 64 位样本。文本必须是以 NUL 结尾的 ASCII；长度、保留位、状态、能力、样本数或成功态失败字段不一致都会拒绝。

主机始终保留 `.mailbox.bin` 和 debug-server 日志。失败状态会报告已完成条目数、失败样本、baseline 和 measured；解析器不会从部分 Mailbox 生成 Profile。

## QEMU system 验证

`--debug-server-kind qemu --measurement-mode qemu_proxy` 使用 `virt`、`-icount` 和 GDB remote 验证完整 Mailbox 链。它适合验证协议和生成器确定性，不代表 BOOM 的流水线、缓存、分支预测或内存系统。
