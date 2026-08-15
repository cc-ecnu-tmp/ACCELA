# ACCELA

ACCELA 是面向 RV64GC/LP64D 的 SysY 编译器。本分支加入目标校准成本调度器 R1 与离线 TargetLab 测量套件；编译器仍使用固定赛事接口：

```sh
./compiler input.sy -S -o output.s -O1
```

目标参数由经过严格校验的 `target-profile.v1` JSON 在构建时生成 Java 源码并嵌入最终编译器。赛事运行时不读取配置文件，不联网，也不接收测试用例标识。开发 Profile 明确标记为 `calibrated: false`，因此不会改变既有生产流水线的收益决策。

## 开发验证

```sh
python -m tools.targetlab validate config/target/boomv3-development.json
python -m tools.targetlab verify-embedded \
  config/target/boomv3-development.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
./gradlew check
```

现场测量从人工合规确认开始，统一流程为 `configure -> build -> run -> collect -> profile -> validate -> embed -> report`。详见：

- [成本调度器架构](docs/cost-scheduler-architecture.zh-CN.md)
- [TargetProfile JSON v1 规范](docs/target-profile-json-v1.zh-CN.md)
- [TargetLab 现场手册](docs/targetlab-field-guide.zh-CN.md)
- [LLVM 对标与发布门禁](docs/benchmark-and-release.zh-CN.md)
