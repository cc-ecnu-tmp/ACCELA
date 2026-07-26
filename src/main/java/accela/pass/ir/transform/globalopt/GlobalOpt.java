package accela.pass.ir.transform;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;

/** Optimizes internal global variables and their accesses. */
public final class GlobalOpt {
  private GlobalOpt() {}

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      Function main = GlobalScalarLocalization.localize(module);
      boolean changed = main != null;
      if (main != null) {
        fam.invalidate(main, PreservedAnalyses.none());
        PromoteMemoryToRegister.run(
            main, fam.getResult(DominatorTreeAnalysis.class, main));
      }
      changed |= GlobalScalarParameterPromotion.runOnModule(module);
      return changed ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
