#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

struct counters {
  uint64_t instructions;
  uint64_t loads;
  uint64_t stores;
};

static struct qemu_plugin_scoreboard *scoreboard;
static qemu_plugin_u64 instruction_count;
static qemu_plugin_u64 load_count;
static qemu_plugin_u64 store_count;
static struct counters result;

static void begin_region(unsigned int cpu, void *data) {
  (void) data;
  qemu_plugin_u64_set(instruction_count, cpu, 0);
  qemu_plugin_u64_set(load_count, cpu, 0);
  qemu_plugin_u64_set(store_count, cpu, 0);
}

static void end_region(unsigned int cpu, void *data) {
  (void) data;
  result.instructions += qemu_plugin_u64_get(instruction_count, cpu);
  result.loads += qemu_plugin_u64_get(load_count, cpu);
  result.stores += qemu_plugin_u64_get(store_count, cpu);
}

static void instrument(qemu_plugin_id_t id, struct qemu_plugin_tb *tb) {
  (void) id;
  size_t count = qemu_plugin_tb_n_insns(tb);
  if (qemu_plugin_insn_vaddr(qemu_plugin_tb_get_insn(tb, 0)) < 0x80000000ULL)
    return;
  for (size_t index = 0; index < count; index++) {
    struct qemu_plugin_insn *instruction = qemu_plugin_tb_get_insn(tb, index);
    uint32_t encoding = 0;
    qemu_plugin_insn_data(instruction, &encoding, sizeof(encoding));
    if (encoding == 0x12300013 || encoding == 0x12400013) {
      qemu_plugin_register_vcpu_insn_exec_cb(
          instruction, encoding == 0x12300013 ? begin_region : end_region,
          QEMU_PLUGIN_CB_NO_REGS, NULL);
      return;
    }
    qemu_plugin_register_vcpu_mem_inline_per_vcpu(
        instruction, QEMU_PLUGIN_MEM_R, QEMU_PLUGIN_INLINE_ADD_U64,
        load_count, 1);
    qemu_plugin_register_vcpu_mem_inline_per_vcpu(
        instruction, QEMU_PLUGIN_MEM_W, QEMU_PLUGIN_INLINE_ADD_U64,
        store_count, 1);
  }
  qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
      tb, QEMU_PLUGIN_INLINE_ADD_U64, instruction_count, count);
}

static void report(qemu_plugin_id_t id, void *data) {
  (void) id;
  (void) data;
  g_autofree char *output = g_strdup_printf(
      "instructions=%" PRIu64 " loads=%" PRIu64 " stores=%" PRIu64 "\n",
      result.instructions, result.loads, result.stores);
  qemu_plugin_outs(output);
  qemu_plugin_scoreboard_free(scoreboard);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv) {
  (void) argc;
  (void) argv;
  if (!info->system_emulation || g_strcmp0(info->target_name, "riscv64"))
    return -1;
  scoreboard = qemu_plugin_scoreboard_new(sizeof(struct counters));
  instruction_count = qemu_plugin_scoreboard_u64_in_struct(
      scoreboard, struct counters, instructions);
  load_count = qemu_plugin_scoreboard_u64_in_struct(
      scoreboard, struct counters, loads);
  store_count = qemu_plugin_scoreboard_u64_in_struct(
      scoreboard, struct counters, stores);
  qemu_plugin_register_vcpu_tb_trans_cb(id, instrument);
  qemu_plugin_register_atexit_cb(id, report, NULL);
  return 0;
}
