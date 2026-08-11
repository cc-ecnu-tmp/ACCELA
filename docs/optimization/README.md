# ACCELA 2026 RISC-V 优化评测手册

本目录是 ACCELA 决赛候选优化的可复现评测入口。目标固定为 RISC-V RV64GC、
LP64D、`medany`，最终评价平台是 BOOM v3。正式主线是“新增一个明确候选，和
冻结基线做同配置配对评测，再组合已合格候选”；允许在独立实验分支实现新的
或实质变更的优化，但不改变 judge-facing 接口：

```sh
java -cp build/classes/java/main Compiler testcase.sy -S -o testcase.s -O1
```

`Compiler` 始终运行当前获准的完整正式 pipeline，默认不产生评测日志。只有开发
入口 `BenchmarkCompiler` 接受实验 profile 和 JSONL remark；未知 pass、非法依赖、
profile 漂移或输出文件缺失都会立即失败。候选在完成全部门禁前保持默认关闭；
关闭既有 pass 的 `without.*` profile 只能进入隔离诊断附录，不能代替新增候选、
决定实现优先级或进入正式收益榜。

## 证据边界

- `compile_only` 只能证明成功生成汇编。
- `qemu_correctness` 证明本地 RV64GC ABI、裸机 runtime、链接和输出校验链可用。
- `qemu_proxy` 比较 QEMU TCG 动态指令、load/store 和固定的 L1D 模型；它不是
  BOOM 周期，也不是官方平台成绩。QEMU 主机 wall-clock 只用于超时和调度。
- `boom_hardware` 才能支持“决赛平台提速”或默认启用实验优化的结论。

所有 correctness gate 都逐字节比较 stdout，并独立比较 `main` 返回值的低八位。
每个物理输入文件通过长度定界的 `opt/accela/sysy-input` fw_cfg item 原样传入；
runner 不追加分隔符、不做文本规范化，也不把 UART stdin 当作 guest 输入。没有
`.in` sidecar 时仍传入显式零字节 item；runtime 在最后一个真实字节之后返回
EOF。程序不必消费到 EOF，未读尾部仍完整保留。任何错误输出、异常退出、非法
汇编、链接失败、verifier 失败或缺失指标都会使该 profile 失去收益排名资格。
超时是删失数据，不能用猜测值补齐。

优化只能依据语义 IR、分析结果和目标属性作决策。禁止依据文件名、函数名、
路径、源码或输入哈希、case ID、字符串指纹以及公开样例常量分派优化。这一
边界同样适用于实验分支和未来决赛配置。

## 已冻结语料

提交的 `benchmark-manifest.v1` 只包含逻辑 ID、相对路径、哈希、大小、来源、
许可证和有效性；
下载包及原始运行留在被忽略的 `.tmp/` 和 `build/`。当前清单如下：

| 角色 | 清单 | 计数与用途 |
|---|---|---|
| B1 | `data/manifests/b1-official-functional-2026.manifest.json` | 140 个唯一功能程序，作为 correctness gate |
| B2 | `data/manifests/b2-family-smoke.manifest.json` | 20 个 family 各 1 例；所有 B1 正确的候选都运行并允许据此调优，但它不是晋级、淘汰或排名门 |
| B3 | `data/manifests/b3-official-performance-2026.manifest.json` | 60 个有效 triplet、20 个 family、22 个唯一源码组；所有完成 B2 调优并冻结的候选都运行，另登记 6 个 orphan sidecar |
| B4 | `data/manifests/b4-official-performance-2025-preliminary.manifest.json` | 2025 年 5 月 59 例历史 holdout；仅 B3 单项 GM 严格大于 1 的冻结候选运行 |
| B5 | `data/manifests/b5-structural-variants.manifest.json` | 20 个 family 各 3 个独立结构/规模变体，共 60 例 |
| B6 | `data/manifests/b6-mature-benchmarks.manifest.json` | 22 个清洁室 workload 的 correctness、small、medium、large，共 88 例 |
| Oracle | `data/manifests/oracle-cleanroom.manifest.json` | 11 个候选族、每族 3 个结构、3 档输入、两条语义等价源码腿，共 99 对/198 例 |

`prime_search1..3` 只有 `.in/.out` 而无源码；B3 将 6 个 sidecar 标为
`packaging_defect` 并排除，绝不合成源码。2025 决赛清单和 2025 功能清单只供
内容审计，顶层有效性为 `excluded/manual_exclusion`，不会进入 campaign 计权。

`data/audits/functional-2026-vs-2025.audit.json` 证明两年的 140 个功能 triplet
内容多重集相同；`data/audits/performance-2026-vs-2025-final.audit.json` 证明
2026 的 60 个有效性能 triplet 与 2025 决赛内容多重集相同，只是命名变化。
因此两批数据各按 2026 可读名称运行和计权一次，不能当作独立样本重复扩权。
`data/audits/performance-2026-vs-2025-preliminary.audit.json` 则确认 B3 与 B4
没有完全相同的 triplet，B4 才能承担历史 holdout 角色。由提交 manifest
重算的官方 B1/B3/B4 与清洁室 B5/B6/Oracle 源码及输入哈希交集为 0。

B2 的选择规则固定为：在 B3 的每个 family 内按输入字节数升序，再按 case ID
稳定排序，选择中位项。20 个锁定 ID 位于
`data/b2-family-smoke-case-ids.txt`。该规则只控制评测调度，不进入编译器，
也不依据运行结果选择样例。

## 官方规则与尚未确认项

`data/source-snapshot.json` 记录官方仓库提交、来源文档和下载包哈希。开始运行
前及生成最终报告前必须各刷新一次官方 `main`，并把固定提交和刷新时间写回
快照：

```sh
git ls-remote https://gitlab.eduxiji.net/csc1/nscscc/compiler2026 refs/heads/main
```

截至当前快照，官方尚未给出决赛 SIMD/向量语义、最终 runtime、完整链接命令、
编译/运行超时和重复测量规则。RV64GC 不含 V 扩展，故 RVV/SIMD 保持
`Blocked`；不能自行补全规则。官方若发布新规范，应先固定其提交与哈希，再重建
测量协议和受影响的运行，不能静默沿用旧假设。

## 环境与构建

在仓库根目录、WSL 或其他 POSIX shell 中运行。需要 Python 3.11+、JDK 21、
`riscv64-elf-gcc`、带 plugin 支持的 `qemu-system-riscv64`、QEMU plugin headers、
GLib 2、`pkg-config` 和 C 编译器。GCC 13.3/Clang 18 对照还需要 Docker。

```sh
python -m venv .tmp/venv
. .tmp/venv/bin/activate
python -m pip install -e '.[test]'
sh gradlew classes --no-daemon
sh scripts/build-qemu-plugins.sh
```

参考前端镜像由 `tools/benchmark/reference-toolchain.Dockerfile` 构建，并在
`data/toolchain-snapshot.json` 中固定 image tag 和 ID。`scripts/reference-compile.sh`
只接受仓库中的这份固定快照。`ACCELA_TOOLCHAIN_SNAPSHOT`、`PYTHON` 以及镜像
tag/ID 环境变量都会被显式拒绝。launcher 固定以 `python3 -I` 隔离模式运行，
因此忽略 `PYTHONPATH`/`PYTHONHOME`；启动后用 `platform.python_version()` 与快照中
规范化的三段式 Python 版本逐字匹配，漂移时在调用 Docker 前失败。其 PATH 解析
结果和版本证据由 campaign preflight 绑定。脚本按 native `docker`、WSL
`docker.exe` 的顺序先执行 daemon readiness；只有已分类的 transport/unreachable
失败才尝试下一候选。首个 reachable daemon 一旦出现镜像缺失、inspect 格式错误
或 ID 不符就立即失败，绝不跨 daemon 搜索另一份镜像；容器始终用精确 image ID
启动。stderr 记录规范化的 `python_mode`/版本、`chosen_cli`/server version、候选
transport 失败和 compiler argv/hash，原始本地 endpoint 诊断不进入提交记录。
formal run 以同一仓库快照作为 compiler artifact，因而同时绑定 driver、adapter、
header、完整 frontend argv、launcher policy、镜像和版本。launcher policy 只有
一个代码常量；reference contract loader 与 campaign toolchain loader 都和快照
逐字段精确比较，缺失或漂移会在编译/计划生成前失败，不能只靠文档或测试声明。
目录型 compiler artifact 先把每个文件名规范化为 POSIX 相对路径，再按该路径的
UTF-8 字节序排序后计算 tree hash；该顺序不依赖 Windows 路径大小写折叠，并与
既有 Linux/WSL Unicode code-point 顺序一致。目录中出现符号链接或非常规文件会
立即失败。仓库 `.gitattributes` 同时把所有 Git 文本固定为 LF；因此 Windows 的
`core.autocrlf` 不会改变 schema、snapshot、plan、wrapper、adapter 或 header 的
物理哈希。评测测试会同时核验 Git `eol` 属性和这些 hash-bound 文件的实际字节。

```sh
docker image inspect accela/reference-toolchain:2026.08.10-cxx1 --format '{{.Id}}'
```

清洁室语料必须能由固定生成器逐字节重建：

```sh
python benchmarks/generate.py --check
python -m unittest discover -s benchmarks/tests -v
python benchmarks/validate.py --interpret-correctness
```

## 清单重建

下面约定下载包已只读放入 `.tmp/official/` 的对应逻辑目录；提交产物从不记录
这些目录的绝对地址。归档 SHA-256 来自 `data/source-snapshot.json`。

```sh
python -m tools.benchmark inventory .tmp/official/2026-riscv-functional \
  --suite-id official-functional-2026 --target rv64gc --data-role B1 \
  --origin-source official-compiler2026:riscv-functional:8e09f56a \
  --origin-snapshot-sha256 98f563c7802a62d57eaa6ea46402ae377ffffdf726eccd86c795bed6cc9fd973 \
  --license-expression NOASSERTION \
  --output docs/optimization/data/manifests/b1-official-functional-2026.manifest.json

python -m tools.benchmark inventory .tmp/official/2026-riscv-performance/performance \
  --suite-id official-performance-2026 --target rv64gc --data-role B3 \
  --origin-source official-compiler2026:riscv-performance:8e09f56a \
  --origin-snapshot-sha256 49fb434e9abff69bca46f0a0a1d9be7811459eeebbd7bd95f87d45d79967e0e6 \
  --license-expression NOASSERTION --ignore-orphans \
  --output docs/optimization/data/manifests/b3-official-performance-2026.manifest.json

python -m tools.benchmark inventory .tmp/official/2026-riscv-performance/performance \
  --source-manifest docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
  --suite-id official-performance-2026-family-smoke --target rv64gc --data-role B2 \
  --origin-source derived:official-performance-2026:family-smoke \
  --case-id-file docs/optimization/data/b2-family-smoke-case-ids.txt \
  --require-one-per-family \
  --output docs/optimization/data/manifests/b2-family-smoke.manifest.json

python -m tools.benchmark inventory .tmp/official/2025-riscv-prelim \
  --suite-id official-performance-2025-preliminary --target rv64gc --data-role B4 \
  --origin-source official-compiler2025:riscv-performance-preliminary \
  --origin-snapshot-sha256 23b32acc70dd6423b729e30d06659fb955bc636fd4eeb01b262e74123e0816c0 \
  --license-expression NOASSERTION \
  --output docs/optimization/data/manifests/b4-official-performance-2025-preliminary.manifest.json

python -m tools.benchmark inventory --cleanroom-manifest benchmarks/manifest.json \
  --suite-id cleanroom-structural-variants-v1 --target rv64gc --data-role B5 \
  --origin-source accela-cleanroom-corpus:v1 --license-expression MIT \
  --output docs/optimization/data/manifests/b5-structural-variants.manifest.json

python -m tools.benchmark inventory --cleanroom-manifest benchmarks/manifest.json \
  --suite-id cleanroom-mature-benchmarks-v1 --target rv64gc --data-role B6 \
  --origin-source accela-cleanroom-corpus:v1 --license-expression MIT \
  --output docs/optimization/data/manifests/b6-mature-benchmarks.manifest.json

python -m tools.benchmark inventory --cleanroom-manifest benchmarks/manifest.json \
  --suite-id cleanroom-oracle-v1 --target rv64gc --data-role oracle \
  --origin-source accela-cleanroom-corpus:v1 --license-expression MIT \
  --output docs/optimization/data/manifests/oracle-cleanroom.manifest.json
```

2025 审计清单使用相同 inventory 接口，但指定
`--validity-status excluded --validity-reason manual_exclusion`：

```sh
python -m tools.benchmark inventory .tmp/official/2025-functional/functional_recover \
  --suite-id audit-functional-2025 --target rv64gc --data-role B1 \
  --origin-source official-compiler2025:functional \
  --origin-snapshot-sha256 7eed5684beb02483727a6747667e161fe650b151377f8fb256d4139d0e3b78de \
  --license-expression NOASSERTION \
  --validity-status excluded --validity-reason manual_exclusion \
  --output docs/optimization/data/manifests/audit-functional-2025.manifest.json

python -m tools.benchmark inventory .tmp/official/2025-riscv-final/RISCV决赛用例 \
  --suite-id audit-performance-2025-final --target rv64gc --data-role B3 \
  --origin-source official-compiler2025:riscv-performance-final \
  --origin-snapshot-sha256 91d1940672d1bd76dd4fc3953a896adcfd7a6f5303a0771218bcf9a9af4b27d6 \
  --license-expression NOASSERTION --ignore-orphans \
  --validity-status excluded --validity-reason manual_exclusion \
  --output docs/optimization/data/manifests/audit-performance-2025-final.manifest.json
```

然后运行跨套件内容审计：

```sh
python -m tools.benchmark audit \
  docs/optimization/data/manifests/b1-official-functional-2026.manifest.json \
  docs/optimization/data/manifests/audit-functional-2025.manifest.json \
  --left-root .tmp/official/2026-riscv-functional \
  --right-root .tmp/official/2025-functional/functional_recover \
  --output docs/optimization/data/audits/functional-2026-vs-2025.audit.json

python -m tools.benchmark audit \
  docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
  docs/optimization/data/manifests/audit-performance-2025-final.manifest.json \
  --left-root .tmp/official/2026-riscv-performance/performance \
  --right-root .tmp/official/2025-riscv-final/RISCV决赛用例 \
  --output docs/optimization/data/audits/performance-2026-vs-2025-final.audit.json

python -m tools.benchmark audit \
  docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
  docs/optimization/data/manifests/b4-official-performance-2025-preliminary.manifest.json \
  --left-root .tmp/official/2026-riscv-performance/performance \
  --right-root .tmp/official/2025-riscv-prelim \
  --output docs/optimization/data/audits/performance-2026-vs-2025-preliminary.audit.json
```

先做无文件访问的 schema 校验，再按语料根目录校验哈希与大小。不同 manifest
根目录不能混用同一个 `--suite-root`：

```sh
python -m tools.benchmark validate schema \
  docs/optimization/data/manifests/*.manifest.json \
  docs/optimization/data/audits/*.audit.json \
  docs/optimization/data/ablation/matrix.json \
  docs/optimization/data/oracle/*.plan.json \
  docs/optimization/data/measurement-protocol.v1.json \
  docs/optimization/data/measurement-protocol.cache-hotblock.v1.json

python -m tools.benchmark validate schema --verify-files \
  --suite-root .tmp/official/2026-riscv-functional \
  docs/optimization/data/manifests/b1-official-functional-2026.manifest.json

python -m tools.benchmark validate schema --verify-files \
  --suite-root .tmp/official/2026-riscv-performance/performance \
  docs/optimization/data/manifests/b2-family-smoke.manifest.json \
  docs/optimization/data/manifests/b3-official-performance-2026.manifest.json

python -m tools.benchmark validate schema --verify-files --suite-root benchmarks \
  docs/optimization/data/manifests/b5-structural-variants.manifest.json \
  docs/optimization/data/manifests/b6-mature-benchmarks.manifest.json \
  docs/optimization/data/manifests/oracle-cleanroom.manifest.json
```

冻结的 `data/campaign/initial.plan.json` 仍绑定当时的 run-record schema hash；当前
语义 validator 会有意拒绝它与 active schema 的漂移，不能把这个拒绝改成执行兼容
层。历史格式只允许用其 `campaign-plan.v1` JSON Schema 做只读结构校验，绝不调用已
移除的旧调度入口，也不刷新原计划中的 schema hash。

## 候选新增主线

Java `PassRegistry.standard()` 是其所在提交中正式 pipeline 的 pass、候选标记、
逻辑族和 occurrence 的唯一事实源。候选筛选基线提交将它冻结导出为
`data/pass-registry.v2.json`；候选实现完成后，当前 standard export 只对应另行命名
和冻结的 executable artifact，不再要求与筛选基线 golden 相等。候选筛选与候选
执行使用两份不同、均不可变的 v2 快照：`screening_base_pass_registry` 是实现候选前
且 candidate lifecycle 数量严格为零的 production pass 集，
`executable_pass_registry` 是筛选合格项实现后、包含 candidate lifecycle 条目的
可执行集合。当前 `data/pass-registry.v2.json` 永久作为 screening base golden
只读保留，不得被候选实现后的 `PassRegistry.standard()` export 覆盖；实现分支必须
把 executable export 写入该 campaign 独立冻结的另一路径。后者过滤掉 candidate
条目后必须与前者按序逐字段一致；新增 candidate 条目必须与 catalog、筛选合格
implementation ID、anchor 和 obligation 完全一致。
两份快照的 canonical SHA-256 预期不同，screening/freeze/final/report 必须分别保留
各自的路径、canonical hash 和 physical hash，禁止用一份 registry 冒充两个阶段。
本轮 `candidate-evaluation-2026-r1` 的后实现事实源固定为：

- `data/candidates/pass-registry.executable-2026-r1.v2.json`：52 项，其中 46 项
  非候选投影逐项等于 screening base，6 项为默认关闭的可执行候选；
- `data/candidates/candidate-catalog.2026-r1.v1.json`：按筛选顺序绑定这 6 项实现、
  stage、显示名和完整 legality obligations；
- `data/candidates/profiles-2026-r1/matrix.json`：只含 candidate-empty 与 6 个
  single profile，不预生成 B3 后才能确定的 Top3 pair。

这些文件一旦进入正式 plan 就是不可变 campaign 输入。B3 后的 pair 必须写入新的
diagnostic profile 目录，不能增补或覆盖 `profiles-2026-r1`。
物理 pipeline profile 使用 `schema_version=2`，以
`enable_candidates` 显式区分 candidate-empty、单候选和双候选 profile。旧
pass-registry/profile v1 只属于冻结历史证据，不得作为当前 candidate campaign
输入。候选主线不再把“关闭哪个既有 pass”当作优化实施路线。
`data/ablation/matrix.json`、`data/campaign/initial.plan.json` 及生成的
`without.*` profile 只保留为历史诊断资产；它们不得启动新的正式 campaign，
也不得产生候选 Top N、晋级决定或默认 pipeline 变更。公开 CLI 已移除旧
`ablate` 生成、调度和执行入口；旧模块与 schema 仅供冻结资产的只读校验。

### 候选身份与实现合同

每个候选必须是一项可审查的编译器变更，而不是 profile 重命名。候选有两个明确
阶段：B2 调优阶段的每次尝试都绑定自己的提交、tree、artifact、profile 和 trial
ID，允许继续修改实现但不得覆盖旧尝试；B2 完成后才冻结用于 B3 的正式 candidate
identity。正式 identity 的以下任一项漂移，都要终止该 campaign 并生成新的冻结
identity，不能复用旧 run：

- 全局唯一的 `candidate_id`、人类可读名称、责任 pass/阶段和实验分支；
- 候选提交、父基线提交、Git tree、compiler artifact tree hash；
- `schema_version=2` 的 candidate-empty profile 和 candidate profile 的物理及
  canonical SHA-256；两者只允许在 `enable_candidates` 上有预期差异；
- 目标语义、适用 IR 形状、合法性前提、拒绝原因和预期受益结构；
- manifest、toolchain、standard/cache-hotblock protocol、runner 和 schema 的
  物理及 canonical 哈希；
- 候选之间的依赖、冲突和组合次序；隐式依赖或按 benchmark 身份分派一律拒绝。

候选代码在本轮始终默认关闭，judge-facing `Compiler ... -O1` 只使用已接受
pipeline。本轮没有 BOOM 实测，因此任何候选都不会因 QEMU 结果自动进入默认
pipeline。开发入口必须为每个候选输出稳定的 considered/applied/rejected 计数和
结构化拒绝原因；不允许吞掉异常、静默改回基线、用文件名/hash/case ID 触发，或
在失败后生成看似成功的空记录。

候选分支上的 baseline profile 必须证明未启用候选时行为没有漂移：B1 汇编与执行
正确，配置哈希匹配，且任何与父基线不一致的 artifact/remark 都有明确解释。
不能用“代码路径理论上没走到”代替实测基线。

### 锁定门禁与唯一收益口径

所有配对使用相同 case、输入字节、编译重复、QEMU 协议、timeout policy 和统计
方法。任一 correctness failure、右删失、缺失 case、配置漂移或不完整 metric 都
使对应候选失去该阶段资格；不能把失败 case 排除后继续计算 GM。

| 阶段 | 必须完成的证据 | 锁定决策 |
|---|---|---|
| Oracle 资格 | 实现前各执行一次覆盖固定 99 pair 的 FULL baseline/optimized run，生成一份按 family/structure/size 索引的 capture；至少一个映射 structure 的 small/medium/large 三档全部完整且正确，并且这三档 speedup 的等权 GM `>= 1.10` | 不满足就不进入实现队列；Oracle 是资格门，不是事后 backlog 注释 |
| 实现与 B1 | 合法性说明、拒绝可观测性、受影响单元测试、整数/浮点 RV64GC E2E、B1 140 例 `qemu_correctness` | 任一失败即停止该 trial 并保留真实失败 |
| B2 调优 | 所有 B1 通过候选都运行 20-family candidate-empty/candidate 配对 | 不设收益阈值，不晋级、不淘汰；允许调优，修改后重跑受影响正确性与 B2，并保留各 trial 身份 |
| 正式冻结 | 固定候选提交/tree、artifact、catalog、筛选基线与可执行两份 pass-registry v2、profile v2、B1--B6 manifest、协议、工具链和 run namespace | 冻结后任何实现或配置变化都必须新建正式 candidate/campaign identity |
| B3 单项 | 所有冻结候选都运行 60 个 official case | 全部正确且单项 B3 GM 严格 `> 1` 才运行 B4--B6 |
| B4--B6 | 合格候选运行 59 + 60 + 88 个 case | 与 B3 合并为 267-case 等权冠军数据集 |
| Top3 诊断 | 按 B3 单候选 GM 取 Top3（不足 3 个则全取），在 B3 最多运行 `C(3,2)=3` 个 pair | pair 只报告交互，不进入 267-case 单候选冠军榜 |
| cache/hotblock | candidate-empty FULL baseline 一次，加 B3 Top3 单候选各一次 | 只作解释；不跑 pair，不进入任何收益 GM |

唯一收益定义是动态指令数的 baseline-over-candidate：

```text
speedup_case = baseline_dynamic_instructions / candidate_dynamic_instructions
```

`speedup > 1` 才是正收益，`speedup = 1` 是持平，`speedup < 1` 是回归。B2、B3、
B4--B6、Oracle 和 pair 必须沿用这个方向；不得再发布 candidate-over-baseline
ratio、反向 `delta_ln` 或另一个“主收益”口径。阶段 GM 只对已冻结清单中的全部
合格 case 等权计算：`GM = exp(mean(ln(speedup_case)))`。load/store、cache miss、
静态 ELF 和 host wall-clock 只能作诊断或 tie-break 中明确列出的静态 text 项，
不能替代动态指令 speedup。

B3--B6 的冠军规则固定且不可在看到结果后调整。只有完成全部 267 case 且全部
正确的单候选可参选；主排序键是 267-case 等权 combined GM。完全相同时依次使用：

1. B3 60-case 等权 GM 较高；
2. 267 个 candidate 二进制的静态 `text` bytes 合计较小；
3. 稳定 `candidate_id` 的字典序较小。

即完整四级顺序为：combined GM、B3 GM、static text bytes、candidate ID。固定
种子 bootstrap 区间、family 聚合、唯一源码组视图和最坏回归仍必须报告，但不
改变这四级冠军规则。QEMU host wall-clock 不进入收益计算。

### 候选交互

Top3 按 B3 单候选 GM 从高到低选择；不足三个时使用全部候选，GM 完全相同则按
稳定 `candidate_id` 字典序。只生成这组候选的两两 profile，所以 B3 最多增加三
个 pair。A+B profile 必须显式列出两个 candidate ID、各自 profile hash、组合
次序和冲突检查。交互沿用唯一 speedup 方向：

```text
interaction_delta_ln =
  ln(speedup_A+B) - ln(speedup_A) - ln(speedup_B)
```

该值描述已实现候选的组合偏离，不等于两个 `without.*` profile 的旧双消融。
pair 仅运行 B3；任何 correctness 或完整性失败都使该 pair 报告为不合格，但 A、
B 的单项记录保持各自结论。pair 不运行 B4--B6 或 cache/hotblock，也不进入单候选
冠军排序。

## 锁定测量协议

QEMU 代理证据继续使用两份严格分离的实物快照：

- `data/measurement-protocol.v1.json` 是候选排名使用的 `standard_proxy`，
  只运行 profile 与 cache plugin；
- `data/measurement-protocol.cache-hotblock.v1.json` 是候选解释使用的
  `cache_hotblock`，额外运行 hotblocks plugin，永不进入收益 GM。

每份协议都绑定源文件、plugin 二进制、QEMU、runner、command/environment、
fw_cfg 输入契约和固定 L1D 模型。正式 run 在启动前重新核验全部物理资产；任何
漂移立即失败。候选与 baseline 必须引用同一个 standard protocol canonical hash。
cache/hotblock 只有在对应 standard run 完整且正确后才可调度，并且只能进入热点
诊断。

输入文件通过长度定界的 `opt/accela/sysy-input` fw_cfg item 原样传入。runner
不得追加分隔符、文本规范化或改用 UART stdin；零输入也必须是显式零字节 item。
静态 ELF 只允许排除固定 4112-byte `.sysy_input_transport` NOBITS scratch；
动态 runtime I/O 只按审计 helper allowlist 排除，不能使用用户可匹配前缀。

正式执行必须先重建 QEMU plugin，再由 candidate driver 用 versioned plan 中的
完整 asset、runner command 和 environment 调用 `protocol verify`。协议要求的
实物一项也不能省略；不得把旧 campaign 的生成参数或 run ID 复制进新脚本。
手工缩写的 verify 命令不构成正式 preflight 证据。

以下 POSIX `sh` 合同是两份协议的可复现 capture/verify 入口。`set --` 与随后各自
添加的 runner 合计恰好覆盖协议要求的 12 个实物；hash 由工具从实物计算，不能
手填：

```sh
set -eu
sh scripts/build-qemu-plugins.sh

repo_root=$(git rev-parse --show-toplevel)
qemu_binary=$(command -v qemu-system-riscv64)
test -n "$qemu_binary" || {
  echo 'qemu-system-riscv64 is unavailable' >&2
  exit 1
}
standard_protocol=docs/optimization/data/measurement-protocol.v1.json
toolchain_snapshot=docs/optimization/data/toolchain-snapshot.json
cache_hotblock_protocol=docs/optimization/data/measurement-protocol.cache-hotblock.v1.json
runner_command='["sh","{runner_executable}","{binary}","{metric_file}","{input}"]'

set -- \
  --asset profile_plugin_source=tools/qemu/profile.c \
  --asset cache_plugin_source=tools/qemu/cache.c \
  --asset hotblocks_plugin_source=tools/qemu/hotblocks.c \
  --asset runtime_filter_source=tools/qemu/runtime-filter.h \
  --asset runtime_source=tools/qemu/runtime.c \
  --asset crt_source=tools/qemu/crt.S \
  --asset linker_script_source=tools/qemu/linker.ld \
  --asset profile_plugin_binary=build/benchmark/qemu-plugins/profile.so \
  --asset cache_plugin_binary=build/benchmark/qemu-plugins/cache.so \
  --asset hotblocks_plugin_binary=build/benchmark/qemu-plugins/hotblocks.so \
  --asset "qemu_binary=$qemu_binary"

python -m tools.benchmark protocol capture \
  --workspace-root "$repo_root" \
  --protocol-id rv64gc-qemu-fwcfg-v1-20260810 \
  --measurement-mode standard_proxy \
  --machine virt --cpu-model default --memory 512M \
  "$@" --asset runner_executable=scripts/benchmark-qemu.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --output "$standard_protocol"

python -m tools.benchmark protocol verify "$standard_protocol" \
  --workspace-root "$repo_root" \
  "$@" --asset runner_executable=scripts/benchmark-qemu.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}'

python -m tools.benchmark protocol capture \
  --workspace-root "$repo_root" \
  --protocol-id rv64gc-qemu-cache-hotblock-fwcfg-v1-20260810 \
  --measurement-mode cache_hotblock \
  --machine virt --cpu-model default --memory 512M \
  "$@" --asset runner_executable=scripts/benchmark-qemu-hotblocks.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --runner-env 'QEMU_HOTBLOCK_PLUGIN={hotblocks_plugin_binary}' \
  --output "$cache_hotblock_protocol"

python -m tools.benchmark protocol verify "$cache_hotblock_protocol" \
  --workspace-root "$repo_root" \
  "$@" --asset runner_executable=scripts/benchmark-qemu-hotblocks.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --runner-env 'QEMU_HOTBLOCK_PLUGIN={hotblocks_plugin_binary}'
```

## Candidate campaign 与运行生命周期

每个 candidate campaign 必须使用从未出现过的 campaign ID、plan hash 和 run ID
namespace。plan 至少绑定 candidate metadata、baseline/candidate profile、B1--B6
manifest、两份协议、toolchain snapshot、run-record schema、Oracle `1.10` 资格门、
B3 `>1` 门和四级冠军规则。B2 trial 允许在自己的身份下调优；正式冻结后候选实现
发生任何变化时必须终止旧 campaign 并重建计划。

正式 campaign 只允许在 WSL 原生 Linux 文件系统的一份 clean checkout 中执行，
不得从 DrvFS/9p 启动。一次 campaign 的 compiler build、raw attempt、status、
normalized record 和报告必须保持同一 workspace identity。虚拟环境、Gradle
输出和 QEMU plugin 在该 checkout 中重建；corpus 迁移必须完成物理 hash 校验。
迁移校验没有完成时只能写明 provenance limitation，不能把 tar 成功等同于证据
等价。

建议顺序是：

1. 对固定 99 pair 各执行一次 FULL baseline/optimized run，形成一份按
   family/structure/size 索引的 Oracle capture；仅让至少一个映射 structure 的
   small/medium/large 三档全部完整且正确、三档 speedup 等权 GM `>=1.10` 的候选
   进入实现；
2. 为实现 trial 生成 candidate-empty/单候选 profile v2，执行 baseline drift 和 B1；
3. 所有 B1 通过候选运行 B2；允许调优，但每次改动保留新 trial 身份并重跑门禁；
4. B2 调优结束后冻结全部候选、规则、语料、工具链、profile 和 campaign identity；
5. 对全部冻结候选运行 B3；仅 B3 GM 严格 `>1` 者运行 B4/B5/B6；
6. 以 267-case 等权 combined GM 和固定 tie-break 选单候选冠军；
7. 对 B3 Top3 最多运行三个 B3 pair；cache/hotblock 只运行 FULL baseline 与 Top3
   单候选各一次；
8. 生成规范化数据、SVG 和中文报告。本轮不运行 BOOM，也不默认启用候选。

### 首份 Oracle 的可复制 terminal run

首次筛选必须先在 clean、已提交的 WSL 原生检出中生成两条 terminal run。下面的
POSIX `sh` 模板把所有公共配置集中在一个函数中；两腿只能改变 `leg`、plan 锁定的
`run_id` 和 normalized output。它不激活虚拟环境：benchmark driver 固定直接调用
`.venv/bin/python`，而 analyzer command 中的 `python` 仍表示 toolchain snapshot
锁定的系统 Python。CLI 没有不落盘的 `--dry-run`；调用 `oracle run` 就会建立不可变
raw evidence，因此所有检查必须先完成。

```sh
set -eu

benchmark_python=.venv/bin/python
test -x "$benchmark_python"

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
test -z "$(git status --porcelain=v1)" || {
  echo 'formal Oracle run requires a clean committed worktree' >&2
  exit 1
}
git diff --check
repo_commit=$(git rev-parse --verify HEAD)

oracle_manifest=docs/optimization/data/manifests/oracle-cleanroom.manifest.json
oracle_suite_root=benchmarks
oracle_plan=docs/optimization/data/oracle/cleanroom-full.plan.json
candidate_evidence=docs/optimization/data/candidates/candidate-evidence.v1.json
candidate_screening_spec=docs/optimization/data/candidates/candidate-screening-spec.v1.json
screening_base_pass_registry=docs/optimization/data/pass-registry.v2.json
full_profile=docs/optimization/profiles/full.json
standard_protocol=docs/optimization/data/measurement-protocol.v1.json

oracle_root=.tmp/runs/candidate-screening-oracle-2026-r1
oracle_raw_state_root=$oracle_root/state
oracle_baseline_run=$oracle_root/baseline.run.json
oracle_optimized_run=$oracle_root/optimized.run.json
oracle_capture_id=candidate-screening-oracle-2026-r1:capture
oracle_capture=docs/optimization/data/candidates/candidate-oracle-capture.v1.json
candidate_screening_id=candidate-screening-2026-r1
candidate_screening=docs/optimization/data/candidates/candidate-screening.v1.json
candidate_screening_report_dir=docs/optimization/data/candidates/screening-report-r1

"$benchmark_python" -m tools.benchmark oracle run --help >/dev/null
"$benchmark_python" -m tools.benchmark validate schema \
  "$oracle_manifest" \
  "$oracle_plan" \
  "$candidate_evidence" \
  "$candidate_screening_spec" \
  "$screening_base_pass_registry" \
  "$standard_protocol" \
  --suite-root "$oracle_suite_root" \
  --verify-files

PYTHONDONTWRITEBYTECODE=1 "$benchmark_python" -B - <<'PY'
from pathlib import Path

from tools.benchmark.schema import load_and_validate, load_pipeline_profile_v2
from tools.benchmark.util import read_json, sha256_file, sha256_json

manifest_path = Path("docs/optimization/data/manifests/oracle-cleanroom.manifest.json")
plan_path = Path("docs/optimization/data/oracle/cleanroom-full.plan.json")
profile_path = Path("docs/optimization/profiles/full.json")
spec_path = Path("docs/optimization/data/candidates/candidate-screening-spec.v1.json")
registry_path = Path("docs/optimization/data/pass-registry.v2.json")
protocol_path = Path("docs/optimization/data/measurement-protocol.v1.json")
snapshot_path = Path("docs/optimization/data/toolchain-snapshot.json")

manifest = load_and_validate(
    manifest_path,
    suite_root=Path("benchmarks"),
    verify_files=True,
)
plan = load_and_validate(plan_path)
profile = load_pipeline_profile_v2(profile_path)
spec = load_and_validate(spec_path)
registry = load_and_validate(registry_path)
protocol = load_and_validate(protocol_path)
snapshot = read_json(snapshot_path)

assert len(manifest["cases"]) == 198
assert len(plan["pairs"]) == 99
assert len({row["family"] for row in plan["pairs"]}) == 11
assert plan["manifest_sha256"] == sha256_json(manifest)
assert plan["baseline_run_id"] == "candidate-screening-oracle-2026-r1:baseline"
assert plan["optimized_run_id"] == "candidate-screening-oracle-2026-r1:optimized"
assert profile == {
    "schema_version": 2,
    "base": "FULL",
    "disable": [],
    "enable_candidates": [],
}
assert plan["pipeline_profile"] == {
    "profile_id": "full",
    "profile_sha256": sha256_file(profile_path),
}
assert spec["pass_registry_sha256"] == sha256_json(registry)
assert not any(row["lifecycle"] == "candidate" for row in registry["passes"])
proxy = snapshot["proxy_execution"]
assert proxy["qemu_system_riscv64"] == "11.0.3"
assert proxy["riscv_bare_metal_linker"] == "15.2.0"
assert proxy["python"] == "3.14.6"
assert proxy["glib"] == "2.88.3"
assert proxy["jdk"] == "21.0.11"
assert (
    proxy["measurement_protocols"]["standard_proxy"]["protocol_sha256"]
    == sha256_json(protocol)
)
PY

sh gradlew clean classes --no-daemon
sh scripts/build-qemu-plugins.sh
test -d build/classes/java/main
test -f build/benchmark/qemu-plugins/profile.so
test -f build/benchmark/qemu-plugins/cache.so
test -f build/benchmark/qemu-plugins/hotblocks.so
test -z "$(git status --porcelain=v1)"

qemu_binary=$(command -v qemu-system-riscv64)
test -n "$qemu_binary"
qemu_version=$("$qemu_binary" --version | awk 'NR == 1 { print $4 }')
linker_version=$(riscv64-elf-gcc -dumpfullversion)
python_version=$(python -c 'import platform; print(platform.python_version())')
glib_version=$(pkg-config --modversion glib-2.0)
jdk_version=$(java -XshowSettings:properties -version 2>&1 |
  awk -F'= ' '/^[[:space:]]*java.version =/ { print $2; exit }')
test "$qemu_version" = 11.0.3
test "$linker_version" = 15.2.0
test "$python_version" = 3.14.6
test "$glib_version" = 2.88.3
test "$jdk_version" = 21.0.11

# benchmark-link.sh 固定上述裸机工具链；正式运行前必须清除交互式覆盖。
unset RISCV_GCC

compiler_command='["sh","scripts/benchmark-compile.sh","{profile}","{source}","{artifact}","{remarks_file}"]'
link_command='["sh","scripts/benchmark-link.sh","{artifact}","{binary}"]'
analyzer_command='["python","-m","tools.benchmark.binary_analyzer","{binary}","--toolchain","accela","--readelf-command","riscv64-elf-readelf","--objdump-command","riscv64-elf-objdump","--remarks","{remarks_file}","--output","{analysis_file}"]'
runner_command='["sh","{runner_executable}","{binary}","{metric_file}","{input}"]'

"$benchmark_python" -m tools.benchmark protocol verify "$standard_protocol" \
  --workspace-root "$repo_root" \
  --asset profile_plugin_source=tools/qemu/profile.c \
  --asset cache_plugin_source=tools/qemu/cache.c \
  --asset hotblocks_plugin_source=tools/qemu/hotblocks.c \
  --asset runtime_filter_source=tools/qemu/runtime-filter.h \
  --asset runtime_source=tools/qemu/runtime.c \
  --asset crt_source=tools/qemu/crt.S \
  --asset linker_script_source=tools/qemu/linker.ld \
  --asset profile_plugin_binary=build/benchmark/qemu-plugins/profile.so \
  --asset cache_plugin_binary=build/benchmark/qemu-plugins/cache.so \
  --asset hotblocks_plugin_binary=build/benchmark/qemu-plugins/hotblocks.so \
  --asset "qemu_binary=$qemu_binary" \
  --asset runner_executable=scripts/benchmark-qemu.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}'

test ! -e "$oracle_root"
mkdir -p "$oracle_root"

run_oracle_leg() {
  "$benchmark_python" -m tools.benchmark oracle run \
    --plan "$oracle_plan" \
    --leg "$1" \
    "$oracle_manifest" \
    --suite-root "$oracle_suite_root" \
    --workspace-root "$repo_root" \
    --output "$2" \
    --state-dir "$oracle_raw_state_root" \
    --run-id "$3" \
    --repo-commit "$repo_commit" \
    --repo-dirty false \
    --pipeline-profile-id full \
    --pipeline-profile-file "$full_profile" \
    --compiler-artifact build/classes/java/main \
    --measurement-protocol "$standard_protocol" \
    --measurement-asset profile_plugin_source=tools/qemu/profile.c \
    --measurement-asset cache_plugin_source=tools/qemu/cache.c \
    --measurement-asset hotblocks_plugin_source=tools/qemu/hotblocks.c \
    --measurement-asset runtime_filter_source=tools/qemu/runtime-filter.h \
    --measurement-asset runtime_source=tools/qemu/runtime.c \
    --measurement-asset crt_source=tools/qemu/crt.S \
    --measurement-asset linker_script_source=tools/qemu/linker.ld \
    --measurement-asset profile_plugin_binary=build/benchmark/qemu-plugins/profile.so \
    --measurement-asset cache_plugin_binary=build/benchmark/qemu-plugins/cache.so \
    --measurement-asset hotblocks_plugin_binary=build/benchmark/qemu-plugins/hotblocks.so \
    --measurement-asset "qemu_binary=$qemu_binary" \
    --measurement-asset runner_executable=scripts/benchmark-qemu.sh \
    --compiler-kind benchmark-compiler \
    --compiler-adapter host \
    --compiler-command-json "$compiler_command" \
    --remarks-file optimization-remarks.jsonl \
    --link-adapter host \
    --link-command-json "$link_command" \
    --analyzer-adapter host \
    --analyzer-command-json "$analyzer_command" \
    --analysis-file binary-analysis.json \
    --runner-kind qemu \
    --runner-adapter host \
    --runner-command-json "$runner_command" \
    --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
    --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
    --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
    --metric-profile rv64gc-qemu-v1 \
    --metric-file metrics.log \
    --compile-timeout 120 \
    --compile-repetitions 5 \
    --link-timeout 120 \
    --analyze-timeout 120 \
    --run-timeout 1800 \
    --timeout-policy initial \
    --timeout-minimum 120 \
    --timeout-multiplier 3 \
    --timeout-cap 1800 \
    --repetitions 1 \
    --jobs 4 \
    --seed 20260809 \
    --artifact-suffix .s \
    --binary-suffix .elf \
    --output-contract lf_return_trailer \
    --environment-label proxy \
    --evidence-level qemu_proxy \
    --tool-version "qemu-system-riscv64=$qemu_version" \
    --tool-version "bare-metal-linker=$linker_version" \
    --tool-version "python=$python_version" \
    --tool-version "glib=$glib_version" \
    --tool-version "accela-jdk=$jdk_version"
}

run_oracle_leg \
  baseline \
  "$oracle_baseline_run" \
  candidate-screening-oracle-2026-r1:baseline
run_oracle_leg \
  optimized \
  "$oracle_optimized_run" \
  candidate-screening-oracle-2026-r1:optimized
```

正式 `scripts/benchmark-link.sh` 固定调用 `riscv64-elf-gcc` 与
`riscv64-elf-readelf`，不接受 ambient `RISCV_GCC` 覆盖。链接显式使用
`-fno-pie -no-pie -static`；产物只有在 readelf 证明其为 `ET_EXEC`、不存在
`PT_INTERP`/`PT_DYNAMIC` 且不含 relocation section 后才可进入后续阶段。
验证失败会删除该次无效 ELF 并使 attempt 终止，不能降级或重试为有效证据。

不得向这两条 pre-implementation Oracle run 传 candidate catalog 或
`--candidate-pass-registry`，也不得加入 `--baseline-timeout-run`、
`baseline_derived`、`--reuse-compile-cache` 或 `--retry-failures`。若正式启动后出现
真实编译、工具、timeout、runtime 或 correctness failure，该 terminal 证据不得删除、
覆盖或重试；只有 execution contract 明确允许的 pre-phase／scheduler interruption
才可继续同一 immutable run。

Oracle capture 和 profile 生成必须走 versioned candidate 入口。下列变量代表冻结
plan 中的 artifact key；调用者先解析并核验对应 physical/canonical hash，不能把
本机绝对路径或临时 run ID 写入提交文件：

```sh
benchmark_python=${benchmark_python:-.venv/bin/python}
test -x "$benchmark_python"
repo_root=$(git rev-parse --show-toplevel)
: "${candidate_evidence:?resolve candidate-evidence.v1}"
: "${candidate_screening_spec:?resolve candidate-screening-spec.v1}"
: "${screening_base_pass_registry:?resolve the pre-implementation registry artifact}"
: "${oracle_plan:?resolve the fixed 99-pair Oracle plan}"
: "${oracle_baseline_run:?resolve the terminal baseline run record}"
: "${oracle_optimized_run:?resolve the terminal optimized run record}"
: "${oracle_raw_state_root:?resolve the shared physical Oracle state root}"
: "${oracle_capture_id:?declare a new Oracle capture identity}"
: "${oracle_capture:?resolve an immutable capture output path}"
: "${candidate_screening_id:?declare a new screening identity}"
: "${candidate_screening:?resolve an immutable screening output path}"
: "${candidate_screening_report_dir:?resolve the first-report output directory}"

"$benchmark_python" -m tools.benchmark candidates oracle-capture \
  --workspace-root "$repo_root" \
  --evidence "$candidate_evidence" \
  --oracle-plan "$oracle_plan" \
  --baseline "$oracle_baseline_run" \
  --optimized "$oracle_optimized_run" \
  --state-root "$oracle_raw_state_root" \
  --capture-id "$oracle_capture_id" \
  --output "$oracle_capture"

"$benchmark_python" -m tools.benchmark candidates screen \
  --workspace-root "$repo_root" \
  --evidence "$candidate_evidence" \
  --spec "$candidate_screening_spec" \
  --pass-registry "$screening_base_pass_registry" \
  --oracle "$oracle_capture" \
  --screening-id "$candidate_screening_id" \
  --output "$candidate_screening" \
  --report "$candidate_screening_report_dir"
```

只有筛选产生非空合格项、且这些项都已实现并导出新的 executable PassRegistry 后，
才运行 profile 生成；第一份筛选报告不依赖下列后实现输入：

```sh
benchmark_python=${benchmark_python:-.venv/bin/python}
test -x "$benchmark_python"
repo_root=$(git rev-parse --show-toplevel)
candidate_registry=${candidate_registry:-docs/optimization/data/candidates/candidate-catalog.2026-r1.v1.json}
executable_pass_registry=${executable_pass_registry:-docs/optimization/data/candidates/pass-registry.executable-2026-r1.v2.json}
candidate_matrix_id=${candidate_matrix_id:-candidate-evaluation-2026-r1-singles}
candidate_profile_directory=${candidate_profile_directory:-docs/optimization/data/candidates/profiles-2026-r1}

"$benchmark_python" -m tools.benchmark candidates profiles \
  --registry "$candidate_registry" \
  --pass-registry "$executable_pass_registry" \
  --workspace-root "$repo_root" \
  --matrix-id "$candidate_matrix_id" \
  --output-dir "$candidate_profile_directory"
```

profiles 入口直接生成 candidate-empty、单候选及显式请求的 pair pipeline profile
v2；candidate-empty 的 `enable_candidates=[]`，单候选必须恰好启用自身，pair 必须
恰好启用两个计划候选。screen 入口在登记 implemented candidate 之前核验覆盖固定
99 pair 的单份 Oracle capture 及其完整 11-family pool。capture 按
family/structure/size 索引；至少一个映射 structure 的 small/medium/large 三档必须
全部完整且正确，并且三档 speedup 的等权 GM `>=1.10`。

筛选映射使用 `eligible_oracle_structure_refs` 的全限定
`{oracle_family_id, structure_id}` 身份，不能仅凭同名 structure 猜测归属或改写
物理 pair。唯一的跨 family 映射是将 `boom_ilp/independent_chains` 用于
`candidate.same-domain-loop-fusion` 资格；它不参与 integer reduction expansion，
但仍以原始 `boom_ilp` family 留在 generator、manifest、plan 与 capture 中。这样
固定 99 对和 11 个物理 Oracle 家族保持不变，也不会产生第 12 类。

Oracle capture 不只读取 normalized run JSON：`--state-root` 必须指向两条 run 共用
的原始 execution-state 根，capture 与后续 screen/freeze 都会在持有既有 lease 时
重放 attempt journal、terminal raw files 和 run record 的 canonical/physical
identity。任一 raw 文件、run 路径、配置或派生 speedup 漂移都使筛选失败，不能靠
重新计算一份内部自洽 JSON 绕过资格门。

实现后的正式调度由 `candidates campaign-plan` 一次绑定六份 manifest、筛选结果、
candidate catalog、筛选基线与可执行两份 pass registry、profile matrix、standard
protocol、clean commit/tree、compiler artifact 和唯一的相对 `raw_state_root`。每次
`candidates campaign-status` 都必须同时给出当时已知的 `--run TASK=RUN_JSON` 和一个
从这些物理 run/state 即时重放生成的唯一 `--raw-evidence-registry` 输出；status 中的
`ready_tasks` 是唯一可执行任务集合。study、freeze、final、status 和 raw registry
均采用 create-or-existing-exact：相同字节可安全重试，不同字节、普通旧文件、父目录
或最终分量 symlink 一律拒绝，不能覆盖既有 campaign 证据。

`candidates analyze` 同样要求 `--raw-state-root`，并从 journal/remark 原件重算 study，
不接受调用者手工提供 remark 摘要。B2 完成后用 `campaign-finalize` 生成 pre-B3
freeze；其输入必须包含从 genesis 到当前 status 的有序完整 ledger。最终报告使用
两阶段闭环：先从 pre-final status 生成 immutable `candidate-final.v1`，再把该 final
作为 `campaign-status --final` 的证据登记出 terminal completed status，最后以完全
相同的 final 输入加上 `--report-output-dir`、terminal
`--report-campaign-status`、完整 `--report-status-ledger` 和 r7 三个只读 root 重放
一次。报告生成器会重新构建整个 final 并要求全文一致；不能用一份未登记进 terminal
ledger 的 JSON 直接发布报告。

正式 B1 和代理 run 的完整候选参数不能省略。candidate driver 从冻结 plan/matrix
解析下列变量后，先做 clean-tree、协议和工具版本 preflight：

```sh
set -eu
test -z "$(git status --porcelain)" || {
  echo 'formal candidate run requires a clean worktree' >&2
  exit 1
}
unset RISCV_GCC
repo_commit=$(git rev-parse HEAD)
repo_root=$(git rev-parse --show-toplevel)
standard_protocol=docs/optimization/data/measurement-protocol.v1.json

: "${candidate_id:?resolve candidate_id from the frozen plan}"
: "${candidate_run_id:?resolve run_id from the frozen plan}"
: "${candidate_profile_id:?resolve profile_id from the frozen matrix}"
: "${candidate_pipeline_profile:?resolve pipeline profile v2 from the frozen matrix}"
: "${candidate_registry:?resolve candidate registry from the frozen plan}"
: "${candidate_pass_registry:?resolve executable PassRegistry v2 from the frozen plan}"
: "${candidate_output:?resolve output from the frozen plan}"
: "${baseline_run:?resolve the same-stage candidate-empty run from the frozen plan}"
: "${candidate_manifest:?resolve manifest from the frozen plan}"
: "${candidate_suite_root:?resolve suite root for the frozen manifest}"

qemu_binary=$(command -v qemu-system-riscv64)
test -n "$qemu_binary" || {
  echo 'qemu-system-riscv64 is unavailable' >&2
  exit 1
}
qemu_version=$("$qemu_binary" --version | awk 'NR == 1 { print $4 }')
linker_version=$(riscv64-elf-gcc -dumpfullversion)
python_version=$(python -c 'import platform; print(platform.python_version())')
glib_version=$(pkg-config --modversion glib-2.0)
jdk_version=$(java -XshowSettings:properties -version 2>&1 |
  awk -F'= ' '/^[[:space:]]*java.version =/ { print $2; exit }')
for observed_version in \
  "$qemu_version" "$linker_version" "$python_version" "$glib_version" "$jdk_version"
do
  test -n "$observed_version" || {
    echo 'failed to observe a required tool version' >&2
    exit 1
  }
done
```

B1 candidate correctness 不采集排名指标，但仍绑定 candidate catalog、物理
pipeline profile v2 和 optimization-remark v2；candidate-empty baseline 使用同一
命令，改用 `enable_candidates=[]` 的 empty profile 和自己的 frozen run ID：

```sh
python -m tools.benchmark validate suite \
  docs/optimization/data/manifests/b1-official-functional-2026.manifest.json \
  --workspace-root "$repo_root" \
  --suite-root .tmp/official/2026-riscv-functional \
  --output "$candidate_output" --state-dir .tmp/runs/state \
  --run-id "$candidate_run_id" \
  --repo-commit "$repo_commit" --repo-dirty false \
  --pipeline-profile-id "$candidate_profile_id" \
  --pipeline-profile-file "$candidate_pipeline_profile" \
  --candidate-registry "$candidate_registry" \
  --candidate-pass-registry "$candidate_pass_registry" \
  --compiler-artifact build/classes/java/main \
  --compiler-command-json '["sh","scripts/benchmark-compile.sh","{profile}","{source}","{artifact}","{remarks_file}"]' \
  --remarks-file optimization-remarks.jsonl \
  --link-command-json '["sh","scripts/benchmark-link.sh","{artifact}","{binary}"]' \
  --runner-command-json '["sh","scripts/benchmark-qemu-correctness.sh","{binary}","{input}"]' \
  --runner-kind qemu --environment-label proxy \
  --evidence-level qemu_correctness --jobs 4 \
  --tool-version "qemu-system-riscv64=$qemu_version" \
  --tool-version "bare-metal-linker=$linker_version" \
  --tool-version "python=$python_version" \
  --tool-version "glib=$glib_version" \
  --tool-version "accela-jdk=$jdk_version"
```

standard proxy run 必须完整列出 12 个 measurement asset，并引用同阶段已完成且全
正确的 candidate-empty baseline 来派生 timeout：

```sh
python -m tools.benchmark run \
  "$candidate_manifest" \
  --workspace-root "$repo_root" \
  --suite-root "$candidate_suite_root" \
  --output "$candidate_output" --state-dir .tmp/runs/state \
  --run-id "$candidate_run_id" \
  --repo-commit "$repo_commit" --repo-dirty false \
  --pipeline-profile-id "$candidate_profile_id" \
  --pipeline-profile-file "$candidate_pipeline_profile" \
  --candidate-registry "$candidate_registry" \
  --candidate-pass-registry "$candidate_pass_registry" \
  --compiler-artifact build/classes/java/main \
  --measurement-protocol "$standard_protocol" \
  --measurement-asset profile_plugin_source=tools/qemu/profile.c \
  --measurement-asset cache_plugin_source=tools/qemu/cache.c \
  --measurement-asset hotblocks_plugin_source=tools/qemu/hotblocks.c \
  --measurement-asset runtime_filter_source=tools/qemu/runtime-filter.h \
  --measurement-asset runtime_source=tools/qemu/runtime.c \
  --measurement-asset crt_source=tools/qemu/crt.S \
  --measurement-asset linker_script_source=tools/qemu/linker.ld \
  --measurement-asset profile_plugin_binary=build/benchmark/qemu-plugins/profile.so \
  --measurement-asset cache_plugin_binary=build/benchmark/qemu-plugins/cache.so \
  --measurement-asset hotblocks_plugin_binary=build/benchmark/qemu-plugins/hotblocks.so \
  --measurement-asset "qemu_binary=$qemu_binary" \
  --measurement-asset runner_executable=scripts/benchmark-qemu.sh \
  --compiler-kind benchmark-compiler \
  --compiler-command-json '["sh","scripts/benchmark-compile.sh","{profile}","{source}","{artifact}","{remarks_file}"]' \
  --remarks-file optimization-remarks.jsonl \
  --link-command-json '["sh","scripts/benchmark-link.sh","{artifact}","{binary}"]' \
  --analyzer-command-json '["python","-m","tools.benchmark.binary_analyzer","{binary}","--toolchain","accela","--readelf-command","riscv64-elf-readelf","--objdump-command","riscv64-elf-objdump","--remarks","{remarks_file}","--output","{analysis_file}"]' \
  --runner-command-json '["sh","{runner_executable}","{binary}","{metric_file}","{input}"]' \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --runner-kind qemu --metric-profile rv64gc-qemu-v1 \
  --run-timeout 1800 --jobs 4 \
  --timeout-policy baseline_derived --baseline-timeout-run "$baseline_run" \
  --environment-label proxy --evidence-level qemu_proxy \
  --tool-version "qemu-system-riscv64=$qemu_version" \
  --tool-version "bare-metal-linker=$linker_version" \
  --tool-version "python=$python_version" \
  --tool-version "glib=$glib_version" \
  --tool-version "accela-jdk=$jdk_version"
```

对 B2/B3/B4/B5/B6 只替换 plan 绑定的 manifest、suite root、run ID、output 和同
阶段 baseline；不得缩写 protocol/assets/configuration。B3 pair 使用
`enable_candidates` 恰含两个 Top3 ID 的物理 pipeline profile v2；不存在另一个
命令行 enablement 来源。cache/hotblock 使用独立协议与 runner，只调度
candidate-empty 和 B3 Top3 单候选，绝不调度 pair。

正式 run 默认使用 attempt-local 冷编译，不读取共享编译缓存。五次编译记录
median/MAD；动态确定性抽样、最大四个 QEMU worker 和 timeout derivation 必须
在 plan 中冻结。baseline-derived timeout 只能引用同协议、同 case 的合格 baseline。
`--retry-failures`、共享 cache 和临时环境覆盖均不得进入正式候选排名。

每个 attempt 依次持久化 phase-start、phase-result 和 terminal journal，再合并
normalized record。已经启动的 attempt 不得补造 identity/journal；真实
compile/link/analyze/runtime/correctness/timeout/measurement failure 永远保留。
run 终态另写入 `run-terminal` append-only hash chain，绑定 run/config/manifest/output、
终态时间、summary 和逐 case commitment，并在 normalized terminal 写入前持久化；
因此零 attempt interruption 也有物理终态来源。缺失、被改写或与 run JSON 不一致的
run-terminal 使 raw verifier 失败，不能从 normalized record 懒补。
只有协议定义的 typed pre-phase/scheduler interruption 才可能在仍活动且方向有效的
candidate campaign 内恢复。若 campaign 因方向错误被冻结，freeze contract 优先：
即使旧 journal 形状在通用 runner 中可恢复，也绝不 resume。

## Oracle、排名与报告

Oracle 是实现前资格门，同时回答“如果某类结构被理想变换，理论上可能有多少
上界”。`data/oracle/cleanroom-full.plan.json` 的两条语义等价源码腿使用完全相同
的 ACCELA pipeline/runtime/metric 配置，并通过 versioned candidate Oracle capture
绑定 run/configuration hash。固定 99 pair 只执行一次 FULL baseline run 和一次
FULL optimized run，形成一份按 family/structure/size 索引的 capture。候选至少有
一个映射 structure 的 small、medium、large 三档全部正确且完整，并按唯一 speedup
方向对这三档计算等权 GM `>=1.10`，才可登记为 qualified 并进入实现。Oracle 仍不能
替代实现后的 B1--B6 配对实测，也不能计入 267-case 冠军 GM。

报告必须严格分开四类结果：

1. 已实现候选冠军榜：只含 B3 GM `>1` 且 B3--B6 共 267 case 完整正确的单候选，
   按固定四级规则排序，并列 official/holdout/结构/成熟 workload 视图、区间、最坏
   回归、代码成本和证据 hash；
2. B3 Top3 pair 交互榜：最多三个 pair，不与单候选 GM 相加，也不参加冠军排序；
3. 候选 Oracle capture 与实现资格：明确标为上界和资格证据，不写成实测提速；
4. 隔离诊断附录：包含旧消融、失败、停止 campaign 和热点解释，不参与任何榜单。

P0/P1/P2/Blocked 可用于尚未实现但已过 Oracle 资格门的实施队列，必须引用 Oracle
plan/capture、三档结构覆盖和合法性证明。已实现候选另按实测 gate 排序，不把
Oracle 上界重复计分。本轮没有 BOOM run；标题和结论只能写 `qemu_proxy`，不得写
“决赛提速”，任何候选都保持默认关闭。

规范化输出至少包括候选/基线 identity、每例和每 suite 指标、正确性、配置与
artifact hash、缺失原因、bootstrap 参数、ranking eligibility。确定性生成的七类
SVG 固定为：单项收益与区间、分 suite 结果、combined 排名、候选交互热力图、
Oracle/捕获率、cache/hotblock、收益/代码大小/风险 Pareto。GCC/Clang gap 进入
独立表格和解释，不伪装成第八类图。每个图中数字都要能追溯到 normalized run ID。

BOOM 仍是未来声称决赛平台提速所需的独立证据级别，但不属于本轮 campaign，也
没有预设一个可由 QEMU 结果替代的默认启用阈值。自动化绿色不能替代项目要求的
人工审查或保护规则。

## 隔离诊断附录：r7 freeze

旧 campaign `accela-rv64gc-finals-2026-r7-0c95767` 因评估方向错误在运行中停止。
它不是候选新增 campaign，分类固定为
`diagnostic_aborted_direction_mismatch`。版本化边界位于：

- `data/diagnostics/diagnostic-freeze.v1.schema.json`
- `data/diagnostics/accela-rv64gc-finals-2026-r7-0c95767.freeze.json`

freeze manifest 物理绑定源提交/tree、r7 plan、两份协议、B1/B3-B2 controller、
status `000..027`、21 行 registry、21 个注册终态 run，以及未注册 partial run
的 16 个成功 case 和 4 个 pending compile journal prefix。四个 pending case
只有已提交的 `phase_started(stage=compile)`，没有 phase-result 或 terminal；
raw compile work product 不扩展 committed prefix。

该 campaign 的固定规则是：

- `diagnostic_only=true`，ranking/promotion/report eligibility 全为 false；
- 自动和手动 resume 均禁止，不追加 `028`，不为 partial run 合成 terminal；
- 不把任何 r7 measurement、study、profile promotion 或 run ID 导入新 campaign；
- 原始目录、持久 lease/output lock 文件和 journal 原位保留；这些文件不是活动
  controller lock，也不得为了“清理状态”而删除；
- 新候选 campaign 必须使用新 ID、plan、run IDs 和 candidate identities；
- 只允许独立重验 hash 后复用 corpus、measurement protocol 和 toolchain；
- 最终报告只能在“隔离诊断附录”提到 r7，不能进入候选收益、交互或 Oracle 榜。

迁移到 WSL 时，完整 ignored-tree 内容 hash 校验曾按用户指示跳过。freeze
manifest 因而只证明其中逐项枚举的当前 WSL artifact，不证明迁移源与整个目标树
完全相同；该限制已经作为 versioned provenance limitation 固化，不能在报告中
省略。

schema 校验不读取或修改 ignored 证据：

```sh
python -I - <<'PY'
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

root = Path("docs/optimization/data/diagnostics")
schema = json.loads((root / "diagnostic-freeze.v1.schema.json").read_text())
instance = json.loads(
    (root / "accela-rv64gc-finals-2026-r7-0c95767.freeze.json").read_text()
)
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)
PY
```

物理复核必须由调用者显式提供 frozen Git tree 和 ignored r7 evidence root，并按
manifest 的 `artifact_namespace.resolution_rule` 解析。不得把本地绝对路径写入
manifest、报告或提交日志。复核失败时保持 freeze 不动并报告具体 artifact key；
不得重新运行旧任务来“修复”哈希。

## Handoff 检查

候选或评测基础设施交付前，逐项确认：

- 当前分支、commit/tree、工作树范围和 candidate/campaign ID；
- candidate baseline drift、B1、B2、B3、B4/B5/B6、交互与 BOOM 各自的真实状态；
- 所有 run/schema/manifest/protocol 的 physical 与 canonical SHA-256；
- registry/status 是否 append-only，失败与 partial journal 是否保持原身份；
- 报告榜单是否严格分开 measured candidate、interaction、Oracle 和 diagnostic；
- 提交内容不含 corpus payload、本地绝对路径、secret、临时环境或 raw run；
- 文档描述与实际验证范围一致；没有用局部测试、QEMU 或 running process 冒充
  BOOM、完整 campaign 或 Release 结论。
