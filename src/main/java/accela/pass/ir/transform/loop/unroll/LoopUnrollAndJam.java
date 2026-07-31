package accela.pass.ir.transform.loop.unroll;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;

/**
 * Unrolls adjacent iterations of a perfect outer loop and jams their inner-loop bodies.
 *
 * <p>The transform is deliberately proof-driven: it requires canonical induction variables,
 * affine non-interfering memory accesses, lane-independent inner control, and enough estimated
 * register capacity. A scalar copy of the original loop handles all remaining iterations.
 */
public final class LoopUnrollAndJam {
  private LoopUnrollAndJam() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (LoopUnrollAndJamCandidate candidate :
        LoopUnrollAndJamCandidate.findAll(function, fam)) {
      var dependences = UnrollAndJamCostModel.analyzeDependences(candidate);
      int factor = UnrollAndJamCostModel.chooseFactor(candidate, dependences);
      if (factor < 2) continue;
      LoopUnrollAndJamTransform.apply(function, candidate, factor, dependences);
      changed = true;
    }
    return changed;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopUnrollAndJam.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
