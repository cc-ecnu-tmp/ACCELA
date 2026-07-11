package accela.pass.ir.transform;

import accela.utils.ir.PromoteMemoryToRegister;
import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Moves scalar globals used only by main into promotable local storage. */
public final class GlobalScalarLocalization {
  private GlobalScalarLocalization() {}

  static boolean runOnModule(accela.ir.Module module) {
    Function main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main"))
        .findFirst().orElse(null);
    if (main == null || main.getEntryBlock() == null) return false;

    List<GlobalVariable> candidates = module.getGlobals().stream()
        .filter(global -> isCandidate(global, main)).toList();
    if (candidates.isEmpty()) return false;

    BasicBlock oldEntry = main.getEntryBlock();
    BasicBlock entry = main.prependBlock("global.local.entry");
    IRBuilder builder = new IRBuilder(entry);
    Map<GlobalVariable, Value> storage = new IdentityHashMap<>();
    for (GlobalVariable global : candidates) {
      storage.put(global, builder.createAlloca(global.getValueType()));
    }
    for (GlobalVariable global : candidates) {
      Value local = storage.get(global);
      builder.createStore(global.getInitializer(), local);
      global.replaceAllUsesWith(local);
    }
    builder.createBr(oldEntry);
    return true;
  }

  private static boolean isCandidate(GlobalVariable global, Function main) {
    if (global.getInitializer() == null || global.getValueType().isArray()
        || !global.hasUses()) return false;
    for (Use use : new ArrayList<>(global.getUses())) {
      Instruction user = use.getUser();
      if (user.getParent() == null || user.getParent().getParent() != main) return false;
      if (user.getOpcode() == Instruction.Opcode.LOAD && user.getOperand(0) == global) continue;
      if (user.getOpcode() == Instruction.Opcode.STORE && user.getOperand(1) == global) continue;
      return false;
    }
    return true;
  }

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      if (!runOnModule(module)) return PreservedAnalyses.all();
      Function main = module.getFunctions().stream()
          .filter(function -> function.getName().equals("main"))
          .findFirst().orElseThrow();
      fam.invalidate(main, PreservedAnalyses.none());
      DominatorTreeAnalysis.Result dominators =
          fam.getResult(DominatorTreeAnalysis.class, main);
      for (Instruction instruction : List.copyOf(main.getEntryBlock().getInstructions())) {
        if (instruction.getOpcode() == Instruction.Opcode.ALLOCA) {
          PromoteMemoryToRegister.run(instruction, dominators);
        }
      }
      return PreservedAnalyses.none();
    }
  }
}
