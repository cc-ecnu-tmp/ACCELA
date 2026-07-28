package accela.pass.ir.transform.recurrence;

import accela.ir.Constant;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import java.util.List;

/** Flat scratch table indexed by the exact runtime state-domain extents. */
record TabulationTable(Type type, GlobalVariable storage, List<Value> rootStates) {
  /** A guarded resource policy, not an assumed bound for any source-level state dimension. */
  static final int MAX_CELLS = 131_072;

  static TabulationTable create(
      accela.ir.Module module, String functionName, List<Value> rootStates) {
    Type type = Type.array(Type.INT, MAX_CELLS);
    GlobalVariable storage =
        new GlobalVariable(
            uniqueName(module, "__rrt_" + functionName + "_values"),
            type,
            Constant.zero(type),
            false);
    module.addGlobal(storage);
    return new TabulationTable(type, storage, List.copyOf(rootStates));
  }

  Value address(IRBuilder builder, List<Value> states) {
    Value flat = states.getFirst();
    for (int index = 1; index < states.size(); index++) {
      Value extent = builder.createAdd(rootStates.get(index), Constant.intConst(1));
      flat = builder.createAdd(builder.createMul(flat, extent), states.get(index));
    }
    Value index = builder.createSExt(flat, Type.I64);
    return builder.createGEP(
        type,
        storage,
        new Value[] {Constant.int64Const(0), index},
        true);
  }

  private static String uniqueName(accela.ir.Module module, String base) {
    String name = base;
    for (int suffix = 1;
        hasGlobal(module, name);
        suffix++) {
      name = base + "." + suffix;
    }
    return name;
  }

  private static boolean hasGlobal(accela.ir.Module module, String name) {
    return module.getGlobals().stream().anyMatch(global -> global.getName().equals(name));
  }
}
