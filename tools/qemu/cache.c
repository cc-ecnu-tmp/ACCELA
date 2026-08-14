#include <qemu-plugin.h>
#include "plugin-compat.h"
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
static uint64_t region_hits;
static uint64_t region_misses;
static uint64_t region_loads;
static uint64_t region_stores;
static uint64_t total_hits;
static uint64_t total_misses;
static uint64_t total_loads;
static uint64_t total_stores;
static struct accela_region_state region;
static void reset_cache(void) {
  for (int set = 0; set < SETS; set++) {
    for (int way = 0; way < WAYS; way++) cache[set][way].valid = false;
  }
  clock_value = 0;
  region_hits = region_misses = region_loads = region_stores = 0;
}
static void begin_main(unsigned int cpu, void *data) {
  (void) cpu;
  (void) data;
  if (accela_region_begin_main(&region)) reset_cache();
}
static void begin_explicit_region(unsigned int cpu, void *data) {
  (void) cpu;
  (void) data;
  if (accela_region_begin_explicit(&region)) reset_cache();
}
static void accumulate(void) {
  total_hits += region_hits;
  total_misses += region_misses;
  total_loads += region_loads;
  total_stores += region_stores;
}
static void end_explicit_region(unsigned int cpu, void *data) {
  (void) cpu;
  (void) data;
  if (accela_region_end_explicit(&region)) accumulate();
}
static void end_main(unsigned int cpu, void *data) {
  (void) cpu;
  (void) data;
  if (accela_region_end_main(&region) == 1) accumulate();
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
  if (region.active == ACCELA_REGION_NONE) return;
  if (qemu_plugin_mem_is_store(info)) region_stores++;
  else region_loads++;
  if (hit) region_hits++;
  else region_misses++;
}

ACCELA_TB_TRANS_DECL(instrument) {
  ACCELA_TB_TRANS_UNUSED_CONTEXT;
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
          : encoding == 0x12400013 ? end_explicit_region
          : encoding == 0x12500013 ? begin_main
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

ACCELA_ATEXIT_DECL(report) {
  ACCELA_ATEXIT_UNUSED_CONTEXT;
  if (!accela_region_complete(&region)) {
    g_autofree char *error = g_strdup_printf(
        "cache_error=%s\n", region.error == NULL ? "unknown" : region.error);
    qemu_plugin_outs(error);
    return;
  }
  uint64_t accesses = total_hits + total_misses;
  g_autofree char *output = g_strdup_printf(
      "l1d=32KiB/8-way/64B accesses=%" PRIu64 " loads=%" PRIu64
      " stores=%" PRIu64 " hits=%" PRIu64 " misses=%" PRIu64
      " miss_rate=%.4f\n",
      accesses, total_loads, total_stores, total_hits, total_misses,
      accesses == 0 ? 0.0 : (double) total_misses / accesses);
  qemu_plugin_outs(output);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv) {
  (void) argc;
  (void) argv;
  if (!info->system_emulation || g_strcmp0(info->target_name, "riscv64"))
    return -1;
  ACCELA_REGISTER_TB_TRANS(id, instrument);
  ACCELA_REGISTER_ATEXIT(id, report);
  return 0;
}
