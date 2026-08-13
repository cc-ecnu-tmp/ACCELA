#include <qemu-plugin.h>
#include "runtime-filter.h"

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

struct block {
  uint64_t address;
  uint64_t executions;
  size_t instructions;
  struct qemu_plugin_scoreboard *scoreboard;
  qemu_plugin_u64 counter;
};

static GPtrArray *blocks;
static struct accela_region_state region;

static void snapshot(unsigned int cpu) {
  for (size_t index = 0; index < blocks->len; index++) {
    struct block *block = g_ptr_array_index(blocks, index);
    block->executions = qemu_plugin_u64_get(block->counter, cpu);
  }
}

static void reset_counters(unsigned int cpu) {
  for (size_t index = 0; index < blocks->len; index++) {
    struct block *block = g_ptr_array_index(blocks, index);
    qemu_plugin_u64_set(block->counter, cpu, 0);
  }
}

static void begin_main(unsigned int cpu, void *data) {
  (void) data;
  if (accela_region_begin_main(&region)) reset_counters(cpu);
}

static void begin_explicit_region(unsigned int cpu, void *data) {
  (void) data;
  if (accela_region_begin_explicit(&region)) reset_counters(cpu);
}

static void end_explicit_region(unsigned int cpu, void *data) {
  (void) data;
  if (accela_region_end_explicit(&region)) snapshot(cpu);
}

static void end_main(unsigned int cpu, void *data) {
  (void) data;
  if (accela_region_end_main(&region) == 1) snapshot(cpu);
}

static void instrument(qemu_plugin_id_t id, struct qemu_plugin_tb *tb) {
  (void) id;
  size_t count = qemu_plugin_tb_n_insns(tb);
  struct qemu_plugin_insn *first = qemu_plugin_tb_get_insn(tb, 0);
  if (qemu_plugin_insn_vaddr(first) < 0x80000000ULL) return;
  for (size_t index = 0; index < count; index++) {
    struct qemu_plugin_insn *instruction = qemu_plugin_tb_get_insn(tb, index);
    uint32_t encoding = 0;
    qemu_plugin_insn_data(instruction, &encoding, sizeof(encoding));
    if (encoding == 0x12300013 || encoding == 0x12400013
        || encoding == 0x12500013 || encoding == 0x12600013) {
      qemu_plugin_vcpu_udata_cb_t callback =
          encoding == 0x12300013 ? begin_explicit_region
          : encoding == 0x12400013 ? end_explicit_region
          : encoding == 0x12500013 ? begin_main
          : end_main;
      qemu_plugin_register_vcpu_insn_exec_cb(
          instruction, callback, QEMU_PLUGIN_CB_NO_REGS, NULL);
      return;
    }
  }
  if (is_io_runtime(first)) return;
  struct block *block = g_new0(struct block, 1);
  block->address = qemu_plugin_insn_vaddr(first);
  block->instructions = count;
  block->scoreboard = qemu_plugin_scoreboard_new(sizeof(uint64_t));
  block->counter = qemu_plugin_scoreboard_u64(block->scoreboard);
  g_ptr_array_add(blocks, block);
  qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
      tb, QEMU_PLUGIN_INLINE_ADD_U64, block->counter, 1);
}

static gint hottest_first(gconstpointer left, gconstpointer right) {
  const struct block *a = *(const struct block *const *) left;
  const struct block *b = *(const struct block *const *) right;
  uint64_t a_dynamic = a->executions * a->instructions;
  uint64_t b_dynamic = b->executions * b->instructions;
  return a_dynamic < b_dynamic ? 1 : a_dynamic > b_dynamic ? -1 : 0;
}

static void report(qemu_plugin_id_t id, void *data) {
  (void) id;
  (void) data;
  if (!accela_region_complete(&region)) {
    g_autofree char *error = g_strdup_printf(
        "hotblocks_error=%s\n", region.error == NULL ? "unknown" : region.error);
    qemu_plugin_outs(error);
    goto cleanup;
  }
  g_ptr_array_sort(blocks, hottest_first);
  size_t limit = MIN(blocks->len, 20);
  for (size_t index = 0; index < limit; index++) {
    const struct block *block = g_ptr_array_index(blocks, index);
    g_autofree char *line = g_strdup_printf(
        "hotblock_rank=%zu address=0x%" PRIx64 " address_decimal=%" PRIu64
        " executions=%" PRIu64 " instructions=%zu dynamic=%" PRIu64 "\n",
        index + 1, block->address, block->address, block->executions,
        block->instructions, block->executions * block->instructions);
    qemu_plugin_outs(line);
  }
cleanup:
  for (size_t index = 0; index < blocks->len; index++) {
    struct block *block = g_ptr_array_index(blocks, index);
    qemu_plugin_scoreboard_free(block->scoreboard);
    g_free(block);
  }
  g_ptr_array_free(blocks, true);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv) {
  (void) argc;
  (void) argv;
  if (!info->system_emulation || g_strcmp0(info->target_name, "riscv64"))
    return -1;
  blocks = g_ptr_array_new();
  qemu_plugin_register_vcpu_tb_trans_cb(id, instrument);
  qemu_plugin_register_atexit_cb(id, report, NULL);
  return 0;
}
