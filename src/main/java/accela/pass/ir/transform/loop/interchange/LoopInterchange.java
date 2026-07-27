package accela.pass.ir.transform.loop.interchange;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;

/**
 * Interchanges tightly nested affine loops when dependence directions and locality permit it.
 */
public final class LoopInterchange {
  private LoopInterchange() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (LoopInterchangeCandidate candidate :
        LoopInterchangeCandidate.findAll(function, fam)) {
      if (!LoopInterchangeProfitability.isProfitable(candidate)) continue;
      LoopInterchangeTransform.apply(candidate);
      changed = true;
    }
    return changed;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopInterchange.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
