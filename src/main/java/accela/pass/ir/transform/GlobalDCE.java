package accela.pass.ir.transform;

import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.ArrayDeque;
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

    Set<Function> reachable = findReachableFunctions(module, main);
    var deadFunctions = module.getFunctions().stream()
        .filter(function -> !reachable.contains(function))
        .toList();
    deadFunctions.forEach(module::removeFunction);

    var deadGlobals = module.getGlobals().stream()
        .filter(global -> !global.hasUses())
        .toList();
    deadGlobals.forEach(module::removeGlobal);
    return !deadFunctions.isEmpty() || !deadGlobals.isEmpty();
  }

  private static Set<Function> findReachableFunctions(
      accela.ir.Module module, Function entry) {
    Set<Function> reachable = Collections.newSetFromMap(new IdentityHashMap<>());
    ArrayDeque<Function> worklist = new ArrayDeque<>();
    reachable.add(entry);
    worklist.add(entry);
    while (!worklist.isEmpty()) {
      Function function = worklist.removeFirst();
      for (var block : function.getBlocks()) {
        for (var instruction : block.getInstructions()) {
          if (instruction.getOpcode() != Instruction.Opcode.CALL) continue;
          Function callee = instruction.getCallee();
          if (callee != null
              && callee.getModule() == module
              && reachable.add(callee)) worklist.addLast(callee);
        }
      }
    }
    return reachable;
  }
}
