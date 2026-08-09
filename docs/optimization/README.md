# ACCELA 2026 RISC-V 优化评测手册

本目录是 ACCELA 决赛优化评测的可复现入口。目标固定为 RISC-V
RV64GC、LP64D、`medany`，最终评价平台是 BOOM v3。当前提交只建立语料、
消融、Oracle、测量和报告基础设施，不新增编译器优化，也不改变评测接口：

```sh
java -cp build/classes/java/main Compiler testcase.sy -S -o testcase.s -O1
```

`Compiler` 始终运行完整正式 pipeline，默认不产生评测日志。只有开发入口
`BenchmarkCompiler` 接受 pipeline profile 和 JSONL remark；未知 pass、非法依赖、
profile 漂移或输出文件缺失都会立即失败。

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
| B2 | `data/manifests/b2-family-smoke.manifest.json` | 20 个 family 各 1 例，运行全部单项消融 |
| B3 | `data/manifests/b3-official-performance-2026.manifest.json` | 60 个有效 triplet、20 个 family、22 个唯一源码组；另登记 6 个 orphan sidecar |
| B4 | `data/manifests/b4-official-performance-2025-preliminary.manifest.json` | 2025 年 5 月 59 例历史 holdout，冻结前不用于阈值调参 |
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
默认从该快照读取两者；`ACCELA_TOOLCHAIN_SNAPSHOT` 可改用另一份已审计快照。
只有 `ACCELA_REFERENCE_IMAGE` 与 `ACCELA_REFERENCE_IMAGE_ID` 同时显式设置时才
不读取快照。脚本会核验 tag 实际 ID；不匹配时失败，不会改用主机上的其他
GCC/Clang。快照同时记录 compile driver 的仓库相对路径和字节哈希；formal run
以该快照作为 compiler artifact，因而同时绑定 driver、镜像和版本。

```sh
docker image inspect accela/reference-toolchain:2026.08.09 --format '{{.Id}}'
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
  docs/optimization/data/campaign/*.plan.json \
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

## Pipeline profile 与消融

`data/pass-registry.v1.json` 是 46 个 pass、28 个逻辑族和 pipeline occurrence 的
快照。必需 lowering、SSA 构造/销毁、寄存器分配和汇编输出只能测成本，不能
作为可关闭优化排名。单项消融默认一次关闭逻辑族的全部 occurrence。

手工入口 `profiles/full.json` 和 `profiles/mandatory-only.json` 便于单例诊断；
正式矩阵从 registry 生成，包含 FULL、mandatory control 和全部 23 个可消融
逻辑族，共 25 个 profile：

```sh
python -m tools.benchmark ablate profiles \
  --registry docs/optimization/data/pass-registry.v1.json \
  --output-dir docs/optimization/data/ablation
```

不能在尚无实测结果时预选 Top 5。B2 与晋级后的 B3 消融完成后，才可按实测
排序把五个稳定 family 传给新的输出目录；工具会生成全部 `5 choose 2 = 10`
个双消融 profile：

```sh
python -m tools.benchmark ablate profiles \
  --registry docs/optimization/data/pass-registry.v1.json \
  --top-family FAMILY_1 --top-family FAMILY_2 --top-family FAMILY_3 \
  --top-family FAMILY_4 --top-family FAMILY_5 \
  --output-dir .tmp/campaign/ablation-top5
```

`metric_without / metric_full` 大于 1 才表示该优化有正贡献。报告使用官方
60-case GM、20-family 聚合、22 个唯一源码组去重结果，以及固定种子
`20260809` 的 10,000 次 family bootstrap。双消融交互定义为双消融的
`delta ln(GM)` 减去两个单消融 `delta ln(GM)` 之和。

## 锁定 QEMU 测量协议

QEMU 代理证据使用两份严格分离的实物快照：

- `data/measurement-protocol.v1.json` 是排名用 `standard_proxy` 协议，只运行
  profile 与 cache plugin；
- `data/measurement-protocol.cache-hotblock.v1.json` 是诊断用
  `cache_hotblock` 协议，另外运行 hotblocks plugin，不参与收益排名。

每份快照都绑定 7 个源文件、3 个 plugin 二进制、QEMU 二进制及版本、自己的
runner 脚本、runner command/environment、输入传输契约，以及 32 KiB、8-way、
64 B line、cold-per-region、LRU 模型。两份快照的 protocol hash 和 runner
command hash 必须不同。每次 `qemu_proxy` run 都重新核验所选协议的全部实物；
任一漂移立即失败。runner 只能通过已核验 asset 占位符取得 QEMU、脚本和
plugin，不能再次从不受控 PATH 或其他目录选取实物。

输入传输遵循 QEMU 官方的
[`fw_cfg` file directory 与 DMA 接口](https://www.qemu.org/docs/master/specs/fw_cfg.html)：
runner 把当前 case 的物理 `{input}` 作为 `opt/accela/sysy-input` file item，
guest 以 4 KiB 分块读取 directory 中记录的无符号 32 位长度，并据此提供精确
EOF；最大输入为 `4294967295` 字节。ELF 只保留固定 4112 B 的
`.sysy_input_transport` NOBITS scratch（16 B DMA descriptor + 4096 B buffer），
不嵌入输入内容。规范化静态 ELF 指标只排除这个固定 scratch；共享 runtime 的
helper text、rodata 和其他状态仍计入静态 text/rodata/data。动态 plugin 则按
精确 allowlist 过滤固定 SysY I/O runtime/helper 基名及其编译器点号后缀，不使用
用户可匹配的宽泛前缀；同前缀的用户函数仍会计数。输入字节数不会通过可变 ELF
嵌入量伪造静态收益，
也不会通过 runtime I/O 开销伪造动态收益。

`data/toolchain-snapshot.json` 的 `measurement_protocols` 同时记录两份协议的
逻辑 mode、相对 path、ID 和 canonical SHA-256。campaign plan 会把这些字段与
实际传入的两份协议逐项交叉核验；字段缺失、路径不一致或哈希漂移都会失败。

以下 POSIX `sh` 命令从最终源文件构建 plugin，然后分别 capture 与 verify 两份
协议。`set --` 中恰好列出协议要求的 12 个实物；不要手填或复制臆测哈希：

```sh
sh scripts/build-qemu-plugins.sh

qemu_binary=$(command -v qemu-system-riscv64)
test -n "$qemu_binary" || {
  echo 'qemu-system-riscv64 is unavailable' >&2
  exit 1
}
standard_protocol=docs/optimization/data/measurement-protocol.v1.json
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
  "$@" --asset runner_executable=scripts/benchmark-qemu.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}'

python -m tools.benchmark protocol capture \
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
  "$@" --asset runner_executable=scripts/benchmark-qemu-hotblocks.sh \
  --runner-command-json "$runner_command" \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --runner-env 'QEMU_HOTBLOCK_PLUGIN={hotblocks_plugin_binary}'
```

## 正确性与代理运行

正式运行应从干净提交开始，原始日志和断点状态写入被忽略目录：

```sh
test -z "$(git status --porcelain)" || {
  echo 'formal benchmark requires a clean worktree' >&2
  exit 1
}
repo_commit=$(git rev-parse HEAD)
campaign_plan=docs/optimization/data/campaign/initial.plan.json
standard_protocol=docs/optimization/data/measurement-protocol.v1.json
cache_hotblock_protocol=docs/optimization/data/measurement-protocol.cache-hotblock.v1.json
campaign_task_field() {
  task_id=$1
  field=$2
  python -m tools.benchmark ablate campaign-task \
    --plan "$campaign_plan" --task-id "$task_id" --field "$field" |
    python -c 'import json, sys
value = json.load(sys.stdin)
if not isinstance(value, str) or not value:
    raise SystemExit("campaign task field is not a non-empty string")
print(value)'
}
campaign_run_id() {
  campaign_task_field "$1" run_id
}

# Record live observations instead of copying version strings into run records.
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

四条性能基线必须使用相同的 QEMU、裸机链接器、Python analyzer 和 GLib
观测值；只有编译器身份可以变化。`--tool-version` 记录实测值，固定容器中的
GCC/Clang 另用 `--official-version` 要求精确匹配。测量协议仍负责逐字节核验
QEMU 与 runtime/plugin 实物，版本字符串不能替代 artifact hash。

B1 correctness gate 不采集排名指标，使用未插 plugin、但与代理协议相同的
exact-input fw_cfg runtime：

```sh
python -m tools.benchmark validate suite \
  docs/optimization/data/manifests/b1-official-functional-2026.manifest.json \
  --suite-root .tmp/official/2026-riscv-functional \
  --output .tmp/runs/b1-full/run.json --state-dir .tmp/runs/state \
  --run-id "$(campaign_run_id 'task:baseline_validation:B1:full-a18b869b2e81:correctness')" \
  --repo-commit "$repo_commit" --repo-dirty false \
  --pipeline-profile-id full \
  --pipeline-profile-file docs/optimization/data/ablation/profiles/full-a18b869b2e81.json \
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

代理 run 使用固定 metric preset。下例中的 runner command/environment 必须与
protocol capture 完全一致，且 `--measurement-asset` 必须覆盖全部 12 个实物：

```sh
python -m tools.benchmark run \
  docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
  --suite-root .tmp/official/2026-riscv-performance/performance \
  --output .tmp/runs/b3-full/run.json --state-dir .tmp/runs/state \
  --run-id "$(campaign_run_id 'task:baseline_validation:B3:full-a18b869b2e81:standard_proxy')" \
  --repo-commit "$repo_commit" --repo-dirty false \
  --pipeline-profile-id full \
  --pipeline-profile-file docs/optimization/data/ablation/profiles/full-a18b869b2e81.json \
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
  --compiler-command-json '["sh","scripts/benchmark-compile.sh","{profile}","{source}","{artifact}","{remarks_file}"]' \
  --remarks-file optimization-remarks.jsonl \
  --link-command-json '["sh","scripts/benchmark-link.sh","{artifact}","{binary}"]' \
  --analyzer-command-json '["python","-m","tools.benchmark.binary_analyzer","{binary}","--toolchain","accela","--readelf-command","riscv64-elf-readelf","--objdump-command","riscv64-elf-objdump","--remarks","{remarks_file}","--output","{analysis_file}"]' \
  --runner-command-json '["sh","{runner_executable}","{binary}","{metric_file}","{input}"]' \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --runner-kind qemu --metric-profile rv64gc-qemu-v1 \
  --timeout-policy initial --run-timeout 1800 --jobs 4 \
  --environment-label proxy --evidence-level qemu_proxy \
  --tool-version "qemu-system-riscv64=$qemu_version" \
  --tool-version "bare-metal-linker=$linker_version" \
  --tool-version "python=$python_version" \
  --tool-version "glib=$glib_version" \
  --tool-version "accela-jdk=$jdk_version"
```

这条命令是四基线中的 ACCELA FULL。ACCELA mandatory-only 使用同一命令和
同一 standard measurement protocol，只替换以下四项：

- `--output .tmp/runs/b3-mandatory/run.json`
- `--run-id "$(campaign_run_id 'task:baseline_validation:B3:mandatory-3e80c8f14208:standard_proxy')"`
- `--pipeline-profile-id mandatory`
- `--pipeline-profile-file docs/optimization/data/ablation/profiles/mandatory-3e80c8f14208.json`

外部参考前端不读取 ACCELA profile。campaign 为固定 flags 生成逻辑 profile
hash，toolchain snapshot 作为 compiler artifact 绑定 compile driver、镜像和
编译器版本；run configuration 同时记录 external compiler command，所以 GCC
和 Clang 身份不会混淆。下列命令可单独执行；纳入 72 小时 campaign 时，必须
另外传入 `--run-id`，值逐字取自 `data/campaign/initial.plan.json` 对应 task，
不得自行另造 ID：

```sh
python -m tools.benchmark run \
  docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
  --suite-root .tmp/official/2026-riscv-performance/performance \
  --output .tmp/runs/b3-gcc-13.3-o2/run.json --state-dir .tmp/runs/state \
  --run-id "$(campaign_run_id 'task:baseline_validation:B3:gcc-13.3-o2-0e6a1017b8f7:standard_proxy')" \
  --repo-commit "$repo_commit" --repo-dirty false \
  --pipeline-profile-id gcc-13.3-o2 \
  --pipeline-profile-sha256 4d06d4b816af821fa0ca86ad8b9b8de59b5b3b8300cb43ebe87b178a7afe29df \
  --compiler-artifact docs/optimization/data/toolchain-snapshot.json \
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
  --compiler-kind external \
  --compiler-command-json '["sh","scripts/reference-compile.sh","gcc","{source}","{artifact}"]' \
  --link-command-json '["sh","scripts/benchmark-link.sh","{artifact}","{binary}"]' \
  --analyzer-command-json '["python","-m","tools.benchmark.binary_analyzer","{binary}","--toolchain","gcc","--readelf-command","riscv64-elf-readelf","--objdump-command","riscv64-elf-objdump","--output","{analysis_file}"]' \
  --runner-command-json '["sh","{runner_executable}","{binary}","{metric_file}","{input}"]' \
  --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
  --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
  --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
  --runner-kind qemu --metric-profile rv64gc-qemu-v1 \
  --timeout-policy initial --run-timeout 1800 --jobs 4 \
  --environment-label proxy --evidence-level qemu_proxy \
  --tool-version "qemu-system-riscv64=$qemu_version" \
  --tool-version "bare-metal-linker=$linker_version" \
  --tool-version "python=$python_version" \
  --tool-version "glib=$glib_version" \
  --tool-version riscv-gcc=13.3.0 \
  --official-version riscv-gcc=13.3.0
```

Clang 基线完整复用上面的 manifest、protocol、assets、link、runner、timeout 和
并发参数，只做以下确定替换；不得把两者并入同一个 run record：

- `--output .tmp/runs/b3-clang-18-o3/run.json`
- `--run-id "$(campaign_run_id 'task:baseline_validation:B3:clang-18-o3-bf83309b138c:standard_proxy')"`
- `--pipeline-profile-id clang-18-o3`
- `--pipeline-profile-sha256 cf167a53c4e164fbe7e60c82f16b858d1c472802181c9177cfee362cf4a24281`
- `--compiler-command-json '["sh","scripts/reference-compile.sh","clang","{source}","{artifact}"]'`
- `--analyzer-command-json '["python","-m","tools.benchmark.binary_analyzer","{binary}","--toolchain","clang","--readelf-command","riscv64-elf-readelf","--objdump-command","riscv64-elf-objdump","--output","{analysis_file}"]'`
- 把 `--tool-version riscv-gcc=13.3.0 --official-version riscv-gcc=13.3.0`
  替换为 `--tool-version clang=18.1.3 --official-version clang=18.1.3`。

正常动态指标每例运行一次；固定种子随机选中的 10% case 自动运行三次并要求
确定性指标完全一致。冷进程编译运行五次，记录 median/MAD。初始单例超时
1800 秒；后续 profile 使用 `min(1800, max(120, 3 * baseline median))`，通过
`--timeout-policy baseline_derived --baseline-timeout-run RUN.json` 绑定基线。
最多并行四个 QEMU 任务。缓存键绑定源码、输入、编译器、profile、工具链和
测量协议哈希；失败重试保留 attempt history，不能吞掉原错误。

GCC 13.3 `-O2` 和 Clang 18 `-O3` 对照通过 `scripts/reference-compile.sh`，共享
RV64GC、LP64D、medany、显式整数 wrap 和严格 FP 选项。它们仍是本地代理，
不是官方平台分数。二进制静态/ELF 指标包含共享代理 runtime，比较时必须使用
同一 runtime 与链接协议。

### B2 全量单项消融

B2 必须先运行 FULL，再按 plan 中的 23 个 singleton task 顺序运行；每个 task
的 run ID、profile ID 和 profile 路径都从冻结 plan 读取，不能由脚本另造。
下面的 POSIX `sh` 流程一次只调度一个 profile，而每个 profile 内由 runner
并行最多四个 QEMU，因此全局并发不会超过四。FULL 使用 1800 秒初始上限；
singleton 逐 case 绑定 B2 FULL，使用 `min(1800, max(120, 3 * baseline))`。

```sh
mkdir -p .tmp/campaign .tmp/runs/b2-singletons
python - "$campaign_plan" > .tmp/campaign/b2-tasks.tsv <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    plan = json.load(stream)
tasks = [task for task in plan["tasks"] if task["phase_id"] == "singleton_b2"]
if len(tasks) != 24 or tasks[0]["kind"] != "full":
    raise SystemExit("campaign plan must contain B2 FULL followed by 23 singleton tasks")
for task in tasks:
    fields = (
        task["task_id"], task["run_id"], task["profile_id"], task["profile_path"]
    )
    if any(value is None or "\t" in value or "\n" in value for value in fields):
        raise SystemExit("campaign task cannot be represented safely")
    print("\t".join(fields))
PY

run_b2_task() {
  profile_id=$1
  profile_path=$2
  run_id=$3
  output=$4
  shift 4
  python -m tools.benchmark run \
    docs/optimization/data/manifests/b2-family-smoke.manifest.json \
    --suite-root .tmp/official/2026-riscv-performance/performance \
    --output "$output" --state-dir .tmp/runs/state --run-id "$run_id" \
    --repo-commit "$repo_commit" --repo-dirty false \
    --pipeline-profile-id "$profile_id" \
    --pipeline-profile-file "docs/optimization/data/ablation/$profile_path" \
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
    --environment-label proxy --evidence-level qemu_proxy \
    --tool-version "qemu-system-riscv64=$qemu_version" \
    --tool-version "bare-metal-linker=$linker_version" \
    --tool-version "python=$python_version" \
    --tool-version "glib=$glib_version" \
    --tool-version "accela-jdk=$jdk_version" \
    "$@"
}

b2_baseline=.tmp/runs/b2-full/run.json
while IFS="$(printf '\t')" read -r task_id run_id profile_id profile_path
do
  if test "$profile_id" = full
  then
    run_b2_task "$profile_id" "$profile_path" "$run_id" "$b2_baseline" \
      --timeout-policy initial
  else
    test -f "$b2_baseline" || {
      echo 'B2 FULL baseline is missing' >&2
      exit 1
    }
    run_b2_task "$profile_id" "$profile_path" "$run_id" \
      ".tmp/runs/b2-singletons/$profile_id/run.json" \
      --timeout-policy baseline_derived --baseline-timeout-run "$b2_baseline"
  fi
done < .tmp/campaign/b2-tasks.tsv
```

失败记录保留在对应 run ID 下；不要通过 `--retry-failures` 把曾经发生过的
correctness、tool 或 timeout 失败洗成可排名结果。修复根因后应创建新的明确
run，而不是覆盖历史证据。

每个 run 会同时持有 output target 与共享 state identity 的非等待 OS 独占锁；
重复 orchestrator 必须立即失败，不能并发改写同一规范化记录或原始目录。上述
run 锁文件永久保留，存活性只由 OS 锁判断，不按 mtime 或 PID 删除所谓 stale lock。每个已
启动 attempt 的原始文件写入独立的 `attempt-XXXX` 目录，resume/retry 只能创建
下一编号；规范化 attempt 通过编号、开始时间和配置哈希与原始 identity 对应。
同一 run 只允许从一个 OS 环境启动；Windows host 与 WSL 对同一文件的跨内核锁
互操作尚未形成验收证据。

### Top 5 cache/hotblock 诊断

cache/hotblock 只在 B3 promotion study 已产生五个合格 profile 后调度。它使用
独立 runner 与协议，并通过 `cache-hotblock-v1` 扩展采集最热块地址、执行次数和
动态指令数。三项规范化值都只接受 `hotblock_rank=1`；原始 Top 20 留在忽略目录，
不进入提交数据。该证据用于解释热点和 pipeline 次序，不进入任何收益 GM。

下面先把 promotion status 中的 Top 5 与冻结 plan 交叉核验，再顺序执行五个
profile。独立协议没有同配置 FULL timeout 基线，因此每个诊断任务使用明确的
initial 1800 秒上限，不把 standard runner 的时间冒充同协议基线：

```sh
test -f .tmp/campaign/status.json || {
  echo 'promotion status is missing' >&2
  exit 1
}
mkdir -p .tmp/campaign .tmp/runs/b3-cache-hotblock
python - "$campaign_plan" .tmp/campaign/status.json \
  > .tmp/campaign/cache-hotblock-tasks.tsv <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    plan = json.load(stream)
with open(sys.argv[2], encoding="utf-8") as stream:
    status = json.load(stream)
selected = set(status["promotion_decisions"]["final_profile_ids"])
if len(selected) != 5:
    raise SystemExit("promotion status must select exactly five profiles")
tasks = [
    task for task in plan["tasks"]
    if task["phase_id"] == "final_validation"
    and task["suite_role"] == "B3"
    and task["measurement_mode"] == "cache_hotblock"
    and task["profile_id"] in selected
]
if len(tasks) != 5 or {task["profile_id"] for task in tasks} != selected:
    raise SystemExit("campaign plan lacks the exact Top 5 cache-hotblock tasks")
for task in sorted(tasks, key=lambda item: item["profile_id"]):
    fields = (
        task["task_id"], task["run_id"], task["profile_id"], task["profile_path"]
    )
    if any(value is None or "\t" in value or "\n" in value for value in fields):
        raise SystemExit("campaign task cannot be represented safely")
    print("\t".join(fields))
PY

run_cache_hotblock_task() {
  profile_id=$1
  profile_path=$2
  run_id=$3
  output=$4
  python -m tools.benchmark run \
    docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
    --suite-root .tmp/official/2026-riscv-performance/performance \
    --output "$output" --state-dir .tmp/runs/state --run-id "$run_id" \
    --repo-commit "$repo_commit" --repo-dirty false \
    --pipeline-profile-id "$profile_id" \
    --pipeline-profile-file "docs/optimization/data/ablation/$profile_path" \
    --compiler-artifact build/classes/java/main \
    --measurement-protocol "$cache_hotblock_protocol" \
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
    --measurement-asset runner_executable=scripts/benchmark-qemu-hotblocks.sh \
    --compiler-kind benchmark-compiler \
    --compiler-command-json '["sh","scripts/benchmark-compile.sh","{profile}","{source}","{artifact}","{remarks_file}"]' \
    --remarks-file optimization-remarks.jsonl \
    --link-command-json '["sh","scripts/benchmark-link.sh","{artifact}","{binary}"]' \
    --analyzer-command-json '["python","-m","tools.benchmark.binary_analyzer","{binary}","--toolchain","accela","--readelf-command","riscv64-elf-readelf","--objdump-command","riscv64-elf-objdump","--remarks","{remarks_file}","--output","{analysis_file}"]' \
    --runner-command-json '["sh","{runner_executable}","{binary}","{metric_file}","{input}"]' \
    --runner-env 'QEMU_SYSTEM_RISCV64={qemu_binary}' \
    --runner-env 'QEMU_PROFILE_PLUGIN={profile_plugin_binary}' \
    --runner-env 'QEMU_CACHE_PLUGIN={cache_plugin_binary}' \
    --runner-env 'QEMU_HOTBLOCK_PLUGIN={hotblocks_plugin_binary}' \
    --runner-kind qemu --metric-profile rv64gc-qemu-v1 \
    --metric-extension cache-hotblock-v1 \
    --timeout-policy initial --run-timeout 1800 --jobs 4 \
    --environment-label proxy --evidence-level qemu_proxy \
    --tool-version "qemu-system-riscv64=$qemu_version" \
    --tool-version "bare-metal-linker=$linker_version" \
    --tool-version "python=$python_version" \
    --tool-version "glib=$glib_version" \
    --tool-version "accela-jdk=$jdk_version"
}

while IFS="$(printf '\t')" read -r task_id run_id profile_id profile_path
do
  output=".tmp/runs/b3-cache-hotblock/$profile_id/run.json"
  mkdir -p "$(dirname "$output")"
  run_cache_hotblock_task "$profile_id" "$profile_path" "$run_id" "$output"
done < .tmp/campaign/cache-hotblock-tasks.tsv
```

## 72 小时调度

固定窗口按 wall-clock 计算，最多四并发：

1. 前 12 小时冻结规则与语料、工具链自检、B1 140 例，以及 B3 60 例的
   ACCELA FULL、mandatory-only、GCC 13.3 `-O2`、Clang 18 `-O3` 四基线；
2. 接着 24 小时在 B2 运行全部 23 个可消融逻辑族；
3. 接着 24 小时把 B2 GM 改善至少 0.5%、任一 case 改善至少 10%、出现超过
   3% 回归或 correctness 异常的族提升到 B3，并至少覆盖实测 Top 8；
4. 最后 12 小时运行实测 Top 5 的 10 个双消融、cache/hotblock、B4、B5、
   B6 和 Oracle。前序未用预算只转入最后阶段。

初始 campaign 不允许预设 Top 5；双消融任务必须等 promotion study 产生实测
Top 5 后才扩展。72 小时截止时，每个缺失项必须保留 run/task ID，并分类为
`not_scheduled`、`timeout`、`tool_failure`、`correctness_failure` 或更具体的
依赖/晋级原因，不能静默删除。

初始计划绑定全部七类 manifest、singleton matrix、paired Oracle plan、两份测量
协议和 reference toolchain snapshot。它会
产生 149 个 task：前 12 小时 5 个、B2 阶段 24 个、B3 晋级阶段 23 个、最终
阶段 97 个；`final_pair_families` 初始为空。

```sh
python -m tools.benchmark ablate campaign-plan \
  --matrix docs/optimization/data/ablation/matrix.json \
  --oracle-plan docs/optimization/data/oracle/cleanroom-full.plan.json \
  --measurement-protocol docs/optimization/data/measurement-protocol.v1.json \
  --cache-hotblock-protocol docs/optimization/data/measurement-protocol.cache-hotblock.v1.json \
  --reference-toolchain docs/optimization/data/toolchain-snapshot.json \
  --workspace-root . \
  --suite B1=docs/optimization/data/manifests/b1-official-functional-2026.manifest.json \
  --suite B2=docs/optimization/data/manifests/b2-family-smoke.manifest.json \
  --suite B3=docs/optimization/data/manifests/b3-official-performance-2026.manifest.json \
  --suite B4=docs/optimization/data/manifests/b4-official-performance-2025-preliminary.manifest.json \
  --suite B5=docs/optimization/data/manifests/b5-structural-variants.manifest.json \
  --suite B6=docs/optimization/data/manifests/b6-mature-benchmarks.manifest.json \
  --suite oracle=docs/optimization/data/manifests/oracle-cleanroom.manifest.json \
  --campaign-id accela-rv64gc-finals-2026 --jobs 4 \
  --output docs/optimization/data/campaign/initial.plan.json

started_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
python -m tools.benchmark ablate campaign-status \
  --plan docs/optimization/data/campaign/initial.plan.json \
  --started-at "$started_at" --output .tmp/campaign/status.json

python -m tools.benchmark ablate campaign-next \
  --plan docs/optimization/data/campaign/initial.plan.json \
  --status .tmp/campaign/status.json

python -m tools.benchmark ablate campaign-task \
  --plan docs/optimization/data/campaign/initial.plan.json \
  --task-id task:baseline_validation:B1:full-a18b869b2e81:correctness \
  --field run_id
```

每次状态刷新用重复的 `--run TASK_ID=RUN.json` 和
`--study singleton_b2|promotion_b3=STUDY.json` 绑定证据，并通过
`--previous-status` 保留 wall-clock 历史。promotion study 选出实测 Top 5 后，
先按上一节生成包含 10 个 pair 的新 matrix，再把 plan、status 和该 matrix
一起 finalize；final plan 会绑定父计划和 promotion status 的哈希：
`campaign-next` 只返回当前 `running` phase 中依赖已满足的任务，不会提前泄露
后续 phase；`campaign-task` 用于逐字段读取冻结身份，找不到或重复时立即失败。

```sh
python -m tools.benchmark ablate campaign-finalize \
  --plan docs/optimization/data/campaign/initial.plan.json \
  --status .tmp/campaign/status.json \
  --matrix .tmp/campaign/ablation-top5/matrix.json \
  --output .tmp/campaign/final.plan.json
```

## Oracle 与报告

Oracle 的 baseline/optimized 两条源码腿都用同一 ACCELA FULL pipeline 编译。
`data/oracle/cleanroom-full.plan.json` 绑定 99 对清洁室证据。Oracle 是候选优化
可达上界，不是 compiler pass 的实测收益；常数 div/rem、现有
`AffineLoopSummarization` 和现有 RRT 只能进入“已实现消融”榜，不能重复列为
新增候选。Oracle plan 的 evidence class 由 manifest data role 推导，不能用
命令行把清洁室证据改标为 official 或 holdout。

最终报告必须严格分开三张榜：已实现优化净贡献、未实现候选 Oracle GM 上界、
决赛后续实施优先级。P0/P1/P2/Blocked 只按公开确定规则排序；候选必须引用
plan hash、run ID、family/pair ID 和明确的合法性证明路径，不能手填收益或把
“证明路径明确”写成“已经证明”。没有 official Oracle 证据的算法候选最多为
P2；依赖未公布 SIMD/ABI/runtime/BOOM 信息的项目为 Blocked。

- P0：official Oracle GM 上界至少 1.02，命中至少两个 official family，在至少
  两个 holdout/成熟 workload 出现相同结构，且合法性证明路径明确。
- P1：official 上界为 1.005--1.02；或者单个 official family 上界至少 1.25，
  且有 holdout 泛化证据。
- P2：低于上述收益、证据不完整、只命中清洁室/公开样例、高风险或工作量过大。
- Blocked：只用于确实依赖未公布 SIMD/ABI/runtime 或 BOOM 实机信息的候选。

同一等级内依次按 official `delta ln(GM)`、holdout 覆盖、实现成本、正确性风险
排序，不使用不可解释的综合分。

```sh
python -m tools.benchmark oracle plan \
  docs/optimization/data/manifests/oracle-cleanroom.manifest.json \
  --suite-root benchmarks --pipeline-profile-id full \
  --pipeline-profile-sha256 bc254fe031aa621711d197384d14e8d88811f8830163661848b8a1f365b7bee0 \
  --output docs/optimization/data/oracle/cleanroom-full.plan.json
```

两条 Oracle 腿必须使用完全相同的 FULL compiler/runtime/metric 配置。下面的
函数按 plan 固定的 run ID 分别执行 99 个 baseline 和 99 个 optimized case；
两侧均使用 initial 1800 秒，避免用一条源码腿的运行时间改变另一条腿的配置。

```sh
run_oracle_leg() {
  leg=$1
  run_id=$2
  output=$3
  python -m tools.benchmark oracle run \
    docs/optimization/data/manifests/oracle-cleanroom.manifest.json \
    --plan docs/optimization/data/oracle/cleanroom-full.plan.json --leg "$leg" \
    --suite-root benchmarks --output "$output" --state-dir .tmp/runs/state \
    --run-id "$run_id" --repo-commit "$repo_commit" --repo-dirty false \
    --pipeline-profile-id full \
    --pipeline-profile-file docs/optimization/data/ablation/profiles/full-a18b869b2e81.json \
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
    --timeout-policy initial --run-timeout 1800 --jobs 4 \
    --environment-label proxy --evidence-level qemu_proxy \
    --tool-version "qemu-system-riscv64=$qemu_version" \
    --tool-version "bare-metal-linker=$linker_version" \
    --tool-version "python=$python_version" \
    --tool-version "glib=$glib_version" \
    --tool-version "accela-jdk=$jdk_version"
}

run_oracle_leg baseline oracle-baseline:cleanroom-oracle-v1 \
  .tmp/runs/oracle-baseline/run.json
run_oracle_leg optimized oracle-optimized:cleanroom-oracle-v1 \
  .tmp/runs/oracle-optimized/run.json
```

Oracle 分析把 baseline 作为分母侧、optimized 作为候选侧：

```sh
python -m tools.benchmark report .tmp/runs/oracle-optimized/run.json \
  --baseline .tmp/runs/oracle-baseline/run.json \
  --baseline-mode pipeline_ablation \
  --oracle-plan docs/optimization/data/oracle/cleanroom-full.plan.json \
  --bootstrap-samples 10000 --seed 20260809 \
  --output-dir .tmp/report/oracle

set -- python -m tools.benchmark report RUN.json \
  --baseline FULL-RUN.json --baseline-mode pipeline_ablation \
  --ablation ABLATION-STUDY.json \
  --oracle-plan docs/optimization/data/oracle/cleanroom-full.plan.json \
  --candidate-evidence CANDIDATE-EVIDENCE.json \
  --candidate-plan CANDIDATE-ORACLE-PLAN.json \
  --candidate-run CANDIDATE-BASELINE-RUN.json \
  --candidate-run CANDIDATE-OPTIMIZED-RUN.json \
  --bootstrap-samples 10000 --seed 20260809 \
  --output-dir .tmp/report

hotblock_count=0
if test -f .tmp/campaign/cache-hotblock-tasks.tsv
then
  while IFS="$(printf '\t')" read -r task_id run_id profile_id profile_path
  do
    hotblock_run=".tmp/runs/b3-cache-hotblock/$profile_id/run.json"
    if test -f "$hotblock_run" &&
      python - "$hotblock_run" "$run_id" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    run = json.load(stream)
if (
    run.get("run_id") != sys.argv[2]
    or run.get("state") != "completed"
    or not run.get("cases")
    or any(case.get("status") != "passed" for case in run["cases"])
):
    raise SystemExit(1)
PY
    then
      set -- "$@" --hotblock-run "$profile_id=$hotblock_run"
      hotblock_count=$((hotblock_count + 1))
    elif test -f "$hotblock_run"
    then
      printf 'omit unsuccessful cache-hotblock run: %s\n' "$run_id" >&2
    fi
  done < .tmp/campaign/cache-hotblock-tasks.tsv
fi
if test "$hotblock_count" -eq 0
then
  printf '%s\n' 'no successful cache-hotblock run; omit hotspot diagnostics' >&2
fi
"$@"
```

报告输出 Markdown、CSV/JSON、总览 `speedups.svg`，以及 waterfall、热图、
toolchain gap、Oracle scaling、收益/成本/风险 Pareto 五张专题 SVG。每个数字
和结论都必须能追溯到
规范化 run ID；未跑完的工作保持显式缺失分类。未来候选从本次审计提交分叉到
`codex/exp-<rank>-<optimization>`，实验实现默认关闭，只有功能、holdout、
QEMU 和最终 BOOM 门全部通过后才进入竞赛配置。真实 BOOM 默认启用门为：
功能全过、官方 GM 至少提升 0.5%、holdout 非负，并且没有超过 5% 的未解释
family 回归。

上面的 `--hotblock-run PROFILE_ID=RUN.json` 只会为实际完成且全 case 正确的 Top 5
诊断逐项追加。至少追加一项时，`summary.json` 的 `hotblock_diagnostics`、
`hotblocks.csv` 和中文 Markdown“热点诊断（不参与收益排名）”共同记录证据；一项
都没有时不生成 `hotblocks.csv`，也不伪造热点数字。
