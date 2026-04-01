package accela.pass.ir.transform;

import accela.ir.Function;
import accela.ir.Module;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;

/**
 * Mem2Reg Promotion
 *
 * <p>The actual SSA-promotion algorithm lives in {@link PromoteMemoryToRegister}; this class only
 * adapts that logic to the pass-manager API and requests the analyses it depends on.
 */
public class Mem2Reg {
  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      DominatorTreeAnalysis.Result domTree =
          fam.getResult(DominatorTreeAnalysis.class, function);
      if (!PromoteMemoryToRegister.run(function, domTree)) {
        return PreservedAnalyses.all();
      }
      return PreservedAnalyses.none().preserve(DominatorTreeAnalysis.class);
    }
  }

  public static void run(Module module) {
    for (Function function : module.getFunctions()) {
      DominatorTreeAnalysis.Result domTree = new DominatorTreeAnalysis().run(function, null);
      PromoteMemoryToRegister.run(function, domTree);
    }
  }
}
