package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Removes functions unreachable from the SysY entry point. */
public final class GlobalDCE {
  private GlobalDCE() {}

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  static boolean runOnModule(accela.ir.Module module) {
    Function main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main"))
        .findFirst()
        .orElse(null);
    if (main == null) return false;

    Set<Function> reachable = Collections.newSetFromMap(new IdentityHashMap<>());
    ArrayDeque<Function> worklist = new ArrayDeque<>();
    worklist.add(main);
    while (!worklist.isEmpty()) {
      Function function = worklist.removeFirst();
      if (!reachable.add(function)) continue;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          Function callee = instruction.getCallee();
          if (instruction.getOpcode() == Instruction.Opcode.CALL
              && callee != null
              && callee.getModule() == module) {
            worklist.addLast(callee);
          }
        }
      }
    }

    boolean changed = false;
    for (Function function : new ArrayList<>(module.getFunctions())) {
      if (reachable.contains(function)) continue;
      for (BasicBlock block : new ArrayList<>(function.getBlocks())) {
        for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
          instruction.eraseFromParent();
        }
        function.removeBlock(block);
      }
      module.removeFunction(function);
      changed = true;
    }
    for (GlobalVariable global : new ArrayList<>(module.getGlobals())) {
      if (global.hasUses()) continue;
      module.removeGlobal(global);
      changed = true;
    }
    return changed;
  }
}
