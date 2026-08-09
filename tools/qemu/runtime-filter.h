#ifndef ACCELA_QEMU_RUNTIME_FILTER_H
#define ACCELA_QEMU_RUNTIME_FILTER_H

enum accela_region_kind {
  ACCELA_REGION_NONE,
  ACCELA_REGION_MAIN,
  ACCELA_REGION_EXPLICIT,
};

struct accela_region_state {
  enum accela_region_kind active;
  bool inside_main;
  bool saw_main;
  bool saw_explicit;
  bool invalid;
  const char *error;
};

static inline bool accela_region_fail(struct accela_region_state *state,
                                      const char *error) {
  if (!state->invalid) state->error = error;
  state->invalid = true;
  return false;
}

static inline bool accela_region_begin_main(struct accela_region_state *state) {
  if (state->invalid) return false;
  if (state->saw_main)
    return accela_region_fail(state, "duplicate_main_start");
  if (state->inside_main || state->active != ACCELA_REGION_NONE)
    return accela_region_fail(state, "nested_main_start");
  state->inside_main = true;
  state->saw_main = true;
  state->active = ACCELA_REGION_MAIN;
  return true;
}

static inline bool accela_region_begin_explicit(
    struct accela_region_state *state) {
  if (state->invalid) return false;
  if (!state->inside_main)
    return accela_region_fail(state, "explicit_start_outside_main");
  if (state->active == ACCELA_REGION_EXPLICIT)
    return accela_region_fail(state, "nested_explicit_start");
  state->saw_explicit = true;
  state->active = ACCELA_REGION_EXPLICIT;
  return true;
}

static inline bool accela_region_end_explicit(
    struct accela_region_state *state) {
  if (state->invalid) return false;
  if (!state->inside_main || state->active != ACCELA_REGION_EXPLICIT)
    return accela_region_fail(state, "explicit_stop_without_start");
  state->active = ACCELA_REGION_NONE;
  return true;
}

/* Returns 1 when the implicit main interval must be accumulated, 0 when
 * explicit intervals already supplied the measurement, and -1 on error. */
static inline int accela_region_end_main(struct accela_region_state *state) {
  if (state->invalid) return -1;
  if (!state->inside_main) {
    accela_region_fail(state, "main_stop_without_start");
    return -1;
  }
  if (state->saw_explicit) {
    if (state->active != ACCELA_REGION_NONE) {
      accela_region_fail(state, "unclosed_explicit_region");
      return -1;
    }
  } else if (state->active != ACCELA_REGION_MAIN) {
    accela_region_fail(state, "invalid_main_region_state");
    return -1;
  }
  int accumulate_main = state->saw_explicit ? 0 : 1;
  state->active = ACCELA_REGION_NONE;
  state->inside_main = false;
  return accumulate_main;
}

static inline bool accela_region_complete(
    struct accela_region_state *state) {
  if (!state->invalid
      && (!state->saw_main || state->inside_main
          || state->active != ACCELA_REGION_NONE)) {
    accela_region_fail(state, "incomplete_main_region");
  }
  return !state->invalid;
}

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
          || !g_strcmp0(symbol, "putfarray")
          || !g_strcmp0(symbol, "_sysy_starttime")
          || !g_strcmp0(symbol, "_sysy_stoptime"));
}

#endif
