package accela.pass.ir.transform;

import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import java.util.List;

/** Promotes scalar globals used only by {@code main} to local SSA values. */
final class GlobalScalarLocalization {
  private GlobalScalarLocalization() {}

  static Function localize(accela.ir.Module module) {
    Function main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main"))
        .findFirst()
        .orElse(null);
    if (main == null || main.getEntryBlock() == null) return null;

    List<GlobalVariable> globals = module.getGlobals().stream()
        .filter(global -> isCandidate(global, main))
        .toList();
    if (globals.isEmpty()) return null;

    var oldEntry = main.getEntryBlock();
    var builder = new IRBuilder(main.prependBlock(uniqueEntryLabel(main)));
    for (GlobalVariable global : globals) {
      Value local = builder.createAlloca(global.getValueType());
      builder.createStore(global.getInitializer(), local);
      global.replaceAllUsesWith(local);
    }
    builder.createBr(oldEntry);
    return main;
  }

  private static String uniqueEntryLabel(Function function) {
    String label = "global.local.entry";
    for (int suffix = 1;
        hasBlockNamed(function, label);
        suffix++) {
      label = "global.local.entry." + suffix;
    }
    return label;
  }

  private static boolean hasBlockNamed(Function function, String label) {
    return function.getBlocks().stream().anyMatch(block -> block.getLabel().equals(label));
  }

  private static boolean isCandidate(GlobalVariable global, Function main) {
    if (global.getInitializer() == null
        || global.getValueType().isArray()
        || global.getValueType().isPointer()
        || !global.hasUses()) return false;
    for (Use use : List.copyOf(global.getUses())) {
      Instruction user = use.getUser();
      if (user.getParent() == null || user.getParent().getParent() != main) return false;
      boolean directLoad =
          user.getOpcode() == Instruction.Opcode.LOAD && user.getOperand(0) == global;
      boolean directStore =
          user.getOpcode() == Instruction.Opcode.STORE && user.getOperand(1) == global;
      if (!directLoad && !directStore) return false;
    }
    return true;
  }

}
