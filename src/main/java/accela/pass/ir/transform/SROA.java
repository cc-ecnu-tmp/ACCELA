package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.transform.sroa.PromoteParameterSlots;
import accela.pass.ir.transform.sroa.PromoteScalarAllocas;
import accela.pass.ir.transform.sroa.ScalarizeArrayAllocas;

/**
 * Thin SROA pass wrapper.
 *
 * <p>The current implementation is intentionally split into small utility transforms:
 *
 * <p>- {@link PromoteParameterSlots}: remove trivial parameter copy slots
 *
 * <p>- {@link ScalarizeArrayAllocas}: split analyzable array allocas into scalar element slots
 *
 * <p>- {@link PromoteScalarAllocas}: aggressively promote the remaining scalar slots to SSA
 */
public class SROA {
  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      DominatorTreeAnalysis.Result domTree =
          fam.getResult(DominatorTreeAnalysis.class, function);
      if (!runOnFunction(function, domTree)) {
        return PreservedAnalyses.all();
      }
      // in-place, does not change the CFG.
      return PreservedAnalyses.none().preserve(DominatorTreeAnalysis.class);
    }
  }

  public static boolean runOnFunction(
      Function function, DominatorTreeAnalysis.Result domTree) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;
    boolean changed = false;
    if (PromoteParameterSlots.runOnFunction(function)) changed = true;
    if (ScalarizeArrayAllocas.runOnFunction(function)) changed = true;
    if (PromoteScalarAllocas.runOnFunction(function, domTree)) changed = true;
    return changed;
  }
}
