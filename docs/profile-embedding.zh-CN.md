# TargetProfile 生成、嵌入与一致性

`profilec` 是原始测量到编译器常量的唯一入口。源 Profile 是严格 JSON；生成物是 `GeneratedTargetProfile.java`。最终 compiler 不包含 JSON/TOML 解析器、不读取环境 Profile，也不提供运行时覆盖。

```sh
python -m tools.targetlab profile collected.json \
  config/target/boomv3-development.json target-profile.json \
  --profile-id field-boomv3-r1
python -m tools.targetlab validate target-profile.json
python -m tools.targetlab embed target-profile.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
python -m tools.targetlab verify-embedded target-profile.json \
  src/main/java/accela/cost/GeneratedTargetProfile.java
./gradlew --offline clean check -PtargetProfile=target-profile.json
```

解析拒绝重复 JSON key、未知字段、NaN/Infinity、缺项、非对称矩阵、单位冲突、样本篡改和证据等级冲突。生成顺序由稳定 instruction class 和上三角 pairing 顺序决定；相同 JSON 必须逐字节生成相同 Java。`verify-embedded` 失败时只能重新生成，不能手改 Java。

`declared` 只允许 `calibrated:false`；`qemu_proxy` 和 `target_hardware` 只允许完整九样本校准归档。证据等级嵌入 Java 并写入 DecisionTrace，防止 QEMU 数据被误报为 BOOM 数据。
