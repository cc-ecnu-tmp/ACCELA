package accela.pass.ir.transform;

import accela.ir.Constant;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.List;

/** Replaces direct loads from scalar constant globals with their initializers. */
public final class GlobalConstantPropagation {
  private GlobalConstantPropagation() {}

  static boolean runOnModule(accela.ir.Module module) {
    boolean changed = false;
    for (GlobalVariable global : module.getGlobals()) {
      Constant initializer = global.getInitializer();
      if (!global.isConstant() || initializer == null
          || global.getValueType().isArray()) continue;
      for (Use use : List.copyOf(global.getUses())) {
        Instruction load = use.getUser();
        if (use.getOperandIndex() != 0
            || load.getOpcode() != Instruction.Opcode.LOAD) continue;
        load.replaceAllUsesWith(initializer);
        load.eraseFromParent();
        changed = true;
      }
    }
    return changed;
  }

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
