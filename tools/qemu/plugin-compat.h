#ifndef ACCELA_QEMU_PLUGIN_COMPAT_H
#define ACCELA_QEMU_PLUGIN_COMPAT_H

/*
 * QEMU 11 bumped the translation and exit callback signatures while keeping
 * the plugin entry point stable.  Keep one source buildable with the older
 * Ubuntu QEMU packages used by the formal evaluator and with current QEMU
 * releases used for local Linux-native preflight.
 */
#if QEMU_PLUGIN_VERSION >= 7
#define ACCELA_TB_TRANS_DECL(name) \
  static void name(struct qemu_plugin_tb *tb, void *userdata)
#define ACCELA_TB_TRANS_UNUSED_CONTEXT (void) userdata
#define ACCELA_REGISTER_TB_TRANS(id, callback) \
  qemu_plugin_register_vcpu_tb_trans_cb((id), (callback), NULL)
#define ACCELA_ATEXIT_DECL(name) static void name(void *userdata)
#define ACCELA_ATEXIT_UNUSED_CONTEXT (void) userdata
#else
#define ACCELA_TB_TRANS_DECL(name) \
  static void name(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
#define ACCELA_TB_TRANS_UNUSED_CONTEXT (void) id
#define ACCELA_REGISTER_TB_TRANS(id, callback) \
  qemu_plugin_register_vcpu_tb_trans_cb((id), (callback))
#define ACCELA_ATEXIT_DECL(name) static void name(qemu_plugin_id_t id, void *userdata)
#define ACCELA_ATEXIT_UNUSED_CONTEXT (void) id; (void) userdata
#endif

#define ACCELA_REGISTER_ATEXIT(id, callback) \
  qemu_plugin_register_atexit_cb((id), (callback), NULL)

#endif
