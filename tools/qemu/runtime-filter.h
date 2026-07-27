#ifndef ACCELA_QEMU_RUNTIME_FILTER_H
#define ACCELA_QEMU_RUNTIME_FILTER_H

static inline bool is_io_runtime(const struct qemu_plugin_insn *instruction) {
  const char *symbol = qemu_plugin_insn_symbol(instruction);
  return symbol != NULL
      && (!g_strcmp0(symbol, "getch")
          || !g_strcmp0(symbol, "getint")
          || !g_strcmp0(symbol, "getfloat")
          || !g_strcmp0(symbol, "getarray")
          || !g_strcmp0(symbol, "getfarray")
          || !g_strcmp0(symbol, "putch")
          || !g_strcmp0(symbol, "putint")
          || !g_strcmp0(symbol, "putarray")
          || !g_strcmp0(symbol, "putfloat")
          || !g_strcmp0(symbol, "putfarray"));
}

#endif
