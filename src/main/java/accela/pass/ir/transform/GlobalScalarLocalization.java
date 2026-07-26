package accela.pass.ir.transform;

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
import java.util.List;

/** Promotes scalar globals used only by {@code main} to local SSA values. */
public final class GlobalScalarLocalization {
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
    var builder = new IRBuilder(main.prependBlock("global.local.entry"));
    for (GlobalVariable global : globals) {
      Value local = builder.createAlloca(global.getValueType());
      builder.createStore(global.getInitializer(), local);
      global.replaceAllUsesWith(local);
    }
    builder.createBr(oldEntry);
    return main;
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

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      Function main = localize(module);
      if (main == null) return PreservedAnalyses.all();

      fam.invalidate(main, PreservedAnalyses.none());
      var dominators = fam.getResult(DominatorTreeAnalysis.class, main);
      PromoteMemoryToRegister.run(main, dominators);
      return PreservedAnalyses.none();
    }
  }
}
