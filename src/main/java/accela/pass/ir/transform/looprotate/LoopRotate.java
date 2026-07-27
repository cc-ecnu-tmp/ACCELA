package accela.pass.ir.transform.looprotate;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.transform.looploadelimination.LoopLoadElimination;
import accela.pass.ir.transform.loopstrengthreduce.LoopStrengthReduce;
import java.util.HashSet;
import java.util.Set;

/** Converts canonical top-tested loops into guarded bottom-tested loops. */
public final class LoopRotate {
  private LoopRotate() {}

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    if (function.getModule() == null) return false;
    GlobalModRefAnalysis.Result modRef =
        GlobalModRefAnalysis.analyze(function.getModule());
    boolean changed = false;
    Set<accela.ir.BasicBlock> rotatedBlocks = new HashSet<>();
    for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
      if (loop.blocks().stream().anyMatch(rotatedBlocks::contains)) continue;
      LoopRotationCandidate candidate = LoopRotationCandidate.match(loop);
      if (candidate == null || !candidate.exposesInvariantPureCall(loop, modRef)) {
        var dominators = fam.getResult(DominatorTreeAnalysis.class, function);
        boolean enablesPointerExit =
            fam.getResult(InductionVariableAnalysis.class, function).inductions().stream()
                .anyMatch(induction -> induction.loop() == loop
                    && LoopStrengthReduce.canOptimizeLoopExit(induction, dominators));
        if (!enablesPointerExit) continue;
        candidate = LoopRotationCandidate.matchForPointerExit(loop);
        if (candidate == null) continue;
      }
      if (!LoopRotation.rotate(function, loop, candidate)) continue;
      rotatedBlocks.addAll(loop.blocks());
      changed = true;
    }
    return changed;
  }

  /** Rotates only loops already proven to expose loop-carried store forwarding. */
  public static boolean runForLoadElimination(
      Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    Set<accela.ir.BasicBlock> rotatedBlocks = new HashSet<>();
    for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
      if (loop.blocks().stream().anyMatch(rotatedBlocks::contains)
          || !LoopLoadElimination.canOptimize(loop)) continue;
      LoopRotationCandidate candidate = LoopRotationCandidate.match(loop);
      if (candidate == null) candidate = LoopRotationCandidate.matchForPointerExit(loop);
      if (candidate == null || !LoopRotation.rotate(function, loop, candidate)) continue;
      rotatedBlocks.addAll(loop.blocks());
      changed = true;
    }
    return changed;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }

  public static final class LoadEliminationPass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runForLoadElimination(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
