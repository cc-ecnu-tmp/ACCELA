# 候选评测 r1 基础设施中止与 Docker r2 迁移说明

日期：2026-08-11

本文固定 `accela-candidate-evaluation-2026-r1` 的证据边界，并规定替代 campaign
`accela-candidate-evaluation-2026-r2` 的 Docker Desktop 迁移条件。r1 因承载工作区
的 WSL ext4 文件系统进入紧急只读状态而中止；这不是候选正确性失败，也不是一个
可以恢复的调度中断。r2 已从本地校验过的 Arch rootfs 归档构建候选工具链镜像，
但尚未生成 plan、status 或 run，也尚未开始任何正式测试。

## 冻结源码身份

r1 与本次救援共同绑定以下源码和 compiler artifact 身份：

- Git commit：`e0a4f7d6c34e9da7f4d52cca406c1a72270af64b`；
- Git tree：`0633f6587e94030f826df3e4af4316ff64b6c6e4`；
- compiler class tree physical SHA-256：
  `77a88ef736b03fdb3b649a5b564ea30eedf5363d8ca1bf99c338fa3a096ae624`。

r2 的文档提交会形成新的 commit/tree，因此必须在该提交上重新构建 compiler
artifact 并把新身份写入 r2 plan。上述 r1 artifact 只用于取证比对，不会直接成为
r2 的执行产物。

## r1 停止点

r1 在同一冻结代码、compiler artifact 和 campaign identity 下留下了以下可读证据：

| 任务 | 可读状态 | 正确性 | candidate decision | 证据用途 |
|---|---|---:|---:|---|
| B1 FULL | terminal succeeded | 140 passed / 140 total | 不适用 | 仅作为 r1 的完整 correctness 记录 |
| B1 `candidate.extended-affine-summarization` | 未封口的 running record | 113 passed / 27 pending / 140 total | 377 rejected / 0 applied | 仅作为中止点诊断 |

第二项在写入不可变 terminal 之前遭遇只读文件系统。`27 pending` 不是失败、超时或
通过；`377 rejected / 0 applied` 只是已持久化前缀中的候选决策计数，不能解释为
完整 B1 捕获率或收益。该记录没有可用于补写 terminal 的合法证据，也没有性能指标。

r1 从此固定为基础设施中止的隔离诊断：

- 不恢复 campaign，不继续其 pending case，也不追加合成 terminal；
- 不把 B1 FULL 的 140/140 迁移成 r2 的门禁结果；r2 必须独立重跑；
- 不把 extended 的 113 个通过 case、377 个 rejection 或任何 journal prefix 导入
  r2 的 normalized record、study、晋级门或报告榜单；
- 不依据 r1 排名、淘汰、调优或启用候选；
- r1 原始证据只可进入最终报告的隔离诊断附录。

## 救援产物

救援集合的相对标识为 `accela-arch-rescue-20260811T120155Z/`。为避免把机器布局写入
仓库，本文只记录集合内相对文件名与 SHA-256：

| 相对文件名 | SHA-256 | 验证 |
|---|---|---|
| `accela-integration-all.bundle` | `0187f4004a81ddc3818cd375ece653f0b2e0f2073c7a8ac27cb0a10eb90928b1` | 源／目标物理哈希一致；`git bundle verify` 完成 |
| `accela-head-e0a4f7d.tar` | `f1ce3d182044ade74ccbdaa51bc573728627020d968009b7fbc550b5654d85a6` | 源／目标物理哈希一致 |
| `failed-campaign-r1.tar.zst` | `b2d493a2a51022c9d7e692508b0c6b3220abe21be94f4b0b5524859bd55cfaf1` | 源／目标物理哈希一致；zstd 完整性测试通过 |
| `official-corpus.tar.zst` | `04819f4c63a68e174de722efbf9ce97fcec22aba7c14911fccf54c3c97b49158` | 源／目标物理哈希一致；zstd 完整性测试通过 |
| `compiler-artifact-r1.tar.zst` | `9ce1d4b287939af5ed4838225eed615dc6a7fdda9ecc985cd793d998502fa522` | 源／目标物理哈希一致；zstd 完整性测试通过 |
| `ignored-control-r1.tar.zst` | `6a6faf673b7392bf3722cbe166cbd886146db8478b2f3e0b249c644424bbfd7d` | 源／目标物理哈希一致；zstd 完整性测试通过 |
| `arch-toolchain-rootfs.tar` | `f0c5cb375e83b3dd661d3b6effd57c3eb3e1000c51fe4897896318d7b57b3055` | Alpine 本地源／Windows 本地目标物理哈希一致；154096 个归档条目通过排除项检查 |
| `arch-toolchain-rootfs.filelist` | `10c4d693195bf94aa0dad6c6a530a7763cf133f0ab8b9360e7619439ca7ce103` | Alpine 本地源／Windows 本地目标物理哈希一致 |

归档复制成功只证明上述字节身份，不会把未封口的 r1 变成 terminal campaign，也
不会赋予其排名资格。恢复时必须从 bundle 校验预期 commit/tree，并在新的 Linux
工作区中独立校验 corpus、协议、registry/profile、toolchain 和 compiler artifact；
不得把归档文件名或宿主机位置作为 campaign identity。

## Docker r2 执行合同

替代 campaign ID 固定为 `accela-candidate-evaluation-2026-r2`。它是一个全新
campaign，而不是 r1 的 resume。正式执行前必须满足：

1. Docker Desktop 内创建专用 Linux named volume。checkout、corpus、compiler
   build、虚拟环境、QEMU plugins、raw attempts、status ledger、normalized records
   和报告都必须位于该 volume；
2. 禁止把 Windows checkout 或证据目录作为正式 workspace bind mount。救援字节
   通过复制或流式导入进入 volume，再在容器内计算并核对物理 SHA-256；
3. 从救援 bundle 建立一份 clean checkout，核对冻结 commit/tree，随后在 volume
   内重建所有 location-bound 构建产物；
4. 独立复核六份 manifest、两份 measurement protocol、toolchain snapshot、
   PassRegistry v2、PipelineProfile v2、候选 catalog/matrix 和 compiler artifact；
5. 生成 r2 自己的 plan、genesis status、run namespace 和 raw-state root。任何 r1
   campaign ID、status、run ID、journal 或 normalized record 都不得成为 r2 输入；
6. preflight 全部通过后，r2 从 B1 FULL 开始按依赖图串行调度 profile，并重新运行
   六个候选的 B1。只有 r2 自己的 terminal 证据能够解锁后续 B2--B6；
7. campaign 终止前不从 volume 汇总或发布正式报告。终止后导出时再次核对规范化
   哈希和物理哈希，且提交产物不得含宿主机绝对路径。

本地 rootfs 归档经 Docker Desktop 导入为
`accela/candidate-toolchain:2026-r2`。镜像 identity 为
`sha256:9e8d0543c848d89cd9bacd6c3cc04859d8ded988cd8f2e3565f0d09bbaedf26d`，
其唯一 rootfs layer digest 与上述归档 SHA-256 相同。只读、无网络、移除 capability
的容器探针已确认 Linux amd64、Python 3.14.6、QEMU 11.0.3、
`riscv64-elf-gcc` 15.2.0、OpenJDK 21.0.11 和 Git 2.55.0 可执行。该结果只证明
救援工具链镜像可启动，不是 compiler build、QEMU correctness 或 formal campaign
证据。

r2 plan hash 和各 run ID 当前仍为 pending。它们只能由 named-volume 内的 clean
checkout 和实际 preflight 生成；本文不预填，也不把计划状态写成已验证状态。

## 报告口径

既有候选筛选报告仍只回答 11 个 Oracle 家族的资格和上界，不代表候选 Pass 的正式
实测收益。r1 也没有产生可排名收益。最终报告必须把三类事实分开：

- Oracle screening：固定结构的理论上界与六项实现资格；
- r2 formal campaign：独立完成的 B1--B6、诊断和 267-case 排名；
- r7 与 r1：互相独立的只读诊断附录，均不进入冠军榜。

在 r2 完整结束前，没有“QEMU 代理下最佳候选”；在 BOOM 硬件证据产生前，也不能
声称决赛平台提速或把实验候选并入默认 judge pipeline。
