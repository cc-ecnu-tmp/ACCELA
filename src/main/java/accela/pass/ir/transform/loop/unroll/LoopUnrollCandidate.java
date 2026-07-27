package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.ExactTripCount;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.List;

/** Legality and trip-count facts for one small full-unroll candidate. */
record LoopUnrollCandidate(
    LoopAnalysis.Loop loop,
    InductionVariableAnalysis.Induction induction,
    Instruction compare,
    BasicBlock body,
    BasicBlock exit,
    int tripCount) {
  private static final int MAX_TRIP_COUNT = 8;

  static LoopUnrollCandidate find(Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops =
        fam.getResult(LoopAnalysis.class, function).loops();
    for (var induction :
        fam.getResult(InductionVariableAnalysis.class, function).inductions()) {
      LoopAnalysis.Loop loop = induction.loop();
      if (hasSubloop(loop, loops)) continue;
      ExactTripCount trip = ExactTripCount.find(induction);
      if (trip == null || trip.count() > MAX_TRIP_COUNT) continue;
      LoopUnrollCandidate candidate = new LoopUnrollCandidate(
          loop, induction, trip.compare(), trip.body(), trip.exit(), trip.count());
      if (LoopUnrollLegality.isSafe(function, candidate)) return candidate;
    }
    return null;
  }

  private static boolean hasSubloop(
      LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> loops) {
    return loops.stream().anyMatch(
        nested -> nested != loop
            && loop.contains(nested.header())
            && nested.blocks().size() < loop.blocks().size());
  }
}
