package accela.pass.ir.transform.sroa;

import accela.ir.ConstantFolding;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Scalarizes the touched leaves of non-escaping global arrays used only by {@code main}. */
public final class ScalarizeGlobalArrays {
  private static final int MAX_SCALAR_LEAVES = 64;

  private ScalarizeGlobalArrays() {}

  public static Function runOnModule(accela.ir.Module module) {
    Function main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main"))
        .findFirst()
        .orElse(null);
    if (main == null || main.getEntryBlock() == null) return null;

    List<Plan> plans = module.getGlobals().stream()
        .map(global -> plan(global, main))
        .filter(java.util.Objects::nonNull)
        .toList();
    if (plans.isEmpty()) return null;

    var oldEntry = main.getEntryBlock();
    IRBuilder builder = new IRBuilder(main.prependBlock("global.array.sroa"));
    for (Plan plan : plans) scalarize(plan, builder);
    builder.createBr(oldEntry);
    return main;
  }

  private static Plan plan(GlobalVariable global, Function main) {
    if (!global.getValueType().isArray() || global.getInitializer() == null) return null;
    Map<Instruction, Integer> accesses = new LinkedHashMap<>();
    Type leafType = arrayLeafType(global.getValueType());
    for (Use use : List.copyOf(global.getUses())) {
      Instruction gep = use.getUser();
      if (use.getOperandIndex() != 0
          || gep.getParent() == null
          || gep.getParent().getParent() != main) return null;
      Integer leaf = ConstantFolding.constantArrayIndex(global, gep);
      if (leaf == null || !hasOnlyMemoryUses(gep, leafType)) return null;
      accesses.put(gep, leaf);
    }
    long touched = accesses.values().stream().distinct().count();
    return touched > 0 && touched <= MAX_SCALAR_LEAVES
        ? new Plan(global, leafType, accesses)
        : null;
  }

  private static boolean hasOnlyMemoryUses(Instruction address, Type leafType) {
    for (Use use : address.getUses()) {
      Instruction user = use.getUser();
      if (user.getOpcode() == Instruction.Opcode.LOAD
          && use.getOperandIndex() == 0
          && user.getType().equals(leafType)) continue;
      if (user.getOpcode() == Instruction.Opcode.STORE
          && use.getOperandIndex() == 1
          && user.getOperand(0).getType().equals(leafType)) continue;
      return false;
    }
    return true;
  }

  private static void scalarize(Plan plan, IRBuilder builder) {
    Map<Integer, Instruction> slots = new LinkedHashMap<>();
    for (int leaf : plan.accesses.values()) {
      if (slots.containsKey(leaf)) continue;
      Instruction slot = builder.createAlloca(plan.leafType);
      builder.createStore(ConstantFolding.initializerAt(plan.global, leaf), slot);
      slots.put(leaf, slot);
    }
    for (var access : plan.accesses.entrySet()) {
      access.getKey().replaceAllUsesWith(slots.get(access.getValue()));
      access.getKey().eraseFromParent();
    }
  }

  private static Type arrayLeafType(Type type) {
    while (type.isArray()) type = type.innerType;
    return type;
  }

  private record Plan(
      GlobalVariable global, Type leafType, Map<Instruction, Integer> accesses) {}

}
