package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;

/**
 * Runs LoopSimplify and LCSSA when the current loops already have canonical CFG shape.
 *
 * <p>The late LICM consumer can benefit from LCSSA on already canonical loops. Forming new edge
 * blocks this late is deliberately avoided: legacy transforms have already run, and adding then
 * removing those blocks can perturb PHI lowering and code layout without enabling an optimization.
 */
public final class LoopCanonicalization {
  private LoopCanonicalization() {}

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    LoopAnalysis.Result loops = fam.getResult(LoopAnalysis.class, function);
    if (loops.loops().isEmpty()
        || loops.loops().stream().anyMatch(loop -> !hasCanonicalCfg(loop))) {
      return false;
    }
    boolean changed = LoopSimplify.runOnFunction(function, fam);
    changed |= LCSSA.runOnFunction(function, fam);
    return changed;
  }

  private static boolean hasCanonicalCfg(LoopAnalysis.Loop loop) {
    if (loop.preheader() == null || loop.latches().size() != 1) return false;
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (loop.contains(successor)) continue;
        if (successor.getPredecessors().stream().anyMatch(
            predecessor -> !loop.contains(predecessor))) {
          return false;
        }
      }
    }
    return true;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
