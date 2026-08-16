#include <qemu-plugin.h>
#include "runtime-filter.h"

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
enum { SETS = 64, WAYS = 8, LINE_SIZE = 64 };
struct line {
  uint64_t tag;
  uint64_t age;
  bool valid;
};
static struct line cache[SETS][WAYS];
static uint64_t clock_value;
static uint64_t hits;
static uint64_t misses;
static uint64_t loads;
static uint64_t stores;
static bool active;
static bool saw_explicit_region;
static void begin_region(unsigned int cpu, void *data) {
  (void) cpu;
  (void) data;
  hits = misses = loads = stores = 0;
  active = true;
}
static void end_region(unsigned int cpu, void *data) {
  (void) cpu;
  (void) data;
  active = false;
}
static void begin_explicit_region(unsigned int cpu, void *data) {
  saw_explicit_region = true;
  begin_region(cpu, data);
}
static void end_main(unsigned int cpu, void *data) {
  if (!saw_explicit_region) end_region(cpu, data);
}
static void access_memory(unsigned int cpu, qemu_plugin_meminfo_t info,
                          uint64_t address, void *data) {
  (void) cpu;
  (void) data;
  uint64_t block = address / LINE_SIZE;
  struct line *set = cache[block % SETS];
  uint64_t tag = block / SETS;
  int victim = 0;
  bool hit = false;
  for (int way = 0; way < WAYS; way++) {
    if (set[way].valid && set[way].tag == tag) {
      victim = way;
      hit = true;
      break;
    }
    if (!set[way].valid || set[way].age < set[victim].age) victim = way;
  }
  set[victim].valid = true;
  set[victim].tag = tag;
  set[victim].age = ++clock_value;
  if (!active) return;
  if (qemu_plugin_mem_is_store(info)) stores++;
  else loads++;
  if (hit) hits++;
  else misses++;
}

#if QEMU_PLUGIN_VERSION >= 7
static void instrument(struct qemu_plugin_tb *tb, void *data) {
  (void) data;
#else
static void instrument(qemu_plugin_id_t id, struct qemu_plugin_tb *tb) {
  (void) id;
#endif
  size_t count = qemu_plugin_tb_n_insns(tb);
  for (size_t index = 0; index < count; index++) {
    struct qemu_plugin_insn *instruction = qemu_plugin_tb_get_insn(tb, index);
    if (qemu_plugin_insn_vaddr(instruction) < 0x80000000ULL) continue;
    uint32_t encoding = 0;
    qemu_plugin_insn_data(instruction, &encoding, sizeof(encoding));
    if (encoding == 0x12300013 || encoding == 0x12400013
        || encoding == 0x12500013 || encoding == 0x12600013) {
      qemu_plugin_vcpu_udata_cb_t callback =
          encoding == 0x12300013 ? begin_explicit_region
          : encoding == 0x12400013 ? end_region
          : encoding == 0x12500013 ? begin_region
          : end_main;
      qemu_plugin_register_vcpu_insn_exec_cb(
          instruction, callback, QEMU_PLUGIN_CB_NO_REGS, NULL);
      continue;
    }
    if (is_io_runtime(instruction)) continue;
    qemu_plugin_register_vcpu_mem_cb(
        instruction, access_memory, QEMU_PLUGIN_CB_NO_REGS,
        QEMU_PLUGIN_MEM_RW, NULL);
  }
}

#if QEMU_PLUGIN_VERSION >= 7
static void report(void *data) {
#else
static void report(qemu_plugin_id_t id, void *data) {
  (void) id;
#endif
  (void) data;
  uint64_t accesses = hits + misses;
  g_autofree char *output = g_strdup_printf(
      "l1d=32KiB/8-way/64B accesses=%" PRIu64 " loads=%" PRIu64
      " stores=%" PRIu64 " hits=%" PRIu64 " misses=%" PRIu64
      " miss_rate=%.4f\n",
      accesses, loads, stores, hits, misses,
      accesses == 0 ? 0.0 : (double) misses / accesses);
  qemu_plugin_outs(output);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv) {
  (void) argc;
  (void) argv;
  if (!info->system_emulation || g_strcmp0(info->target_name, "riscv64"))
    return -1;
#if QEMU_PLUGIN_VERSION >= 7
  qemu_plugin_register_vcpu_tb_trans_cb(id, instrument, NULL);
#else
  qemu_plugin_register_vcpu_tb_trans_cb(id, instrument);
#endif
  qemu_plugin_register_atexit_cb(id, report, NULL);
  return 0;
}
