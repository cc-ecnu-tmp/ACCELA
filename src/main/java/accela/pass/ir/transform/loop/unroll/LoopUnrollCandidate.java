package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import java.math.BigInteger;
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
    ScalarEvolutionAnalysis.Result scalarEvolution =
        fam.getResult(ScalarEvolutionAnalysis.class, function);
    for (var induction :
        fam.getResult(InductionVariableAnalysis.class, function).inductions()) {
      LoopAnalysis.Loop loop = induction.loop();
      if (hasSubloop(loop, loops)) continue;
      LoopExit exit = matchLoopExit(loop);
      if (exit == null) continue;
      BigInteger count = scalarEvolution
          .getConstantBackedgeTakenCount(loop)
          .orElse(null);
      if (count == null
          || count.signum() <= 0
          || count.compareTo(BigInteger.valueOf(MAX_TRIP_COUNT)) > 0) continue;
      LoopUnrollCandidate candidate = new LoopUnrollCandidate(
          loop, induction, exit.compare(), exit.body(), exit.exit(), count.intValueExact());
      if (LoopUnrollLegality.isSafe(function, candidate)) return candidate;
    }
    return null;
  }

  private static LoopExit matchLoopExit(LoopAnalysis.Loop loop) {
    Instruction branch = loop.header().getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) return null;
    BasicBlock trueTarget = (BasicBlock) branch.getOperand(1);
    BasicBlock falseTarget = (BasicBlock) branch.getOperand(2);
    if (loop.contains(trueTarget) == loop.contains(falseTarget)) return null;
    return loop.contains(trueTarget)
        ? new LoopExit(compare, trueTarget, falseTarget)
        : new LoopExit(compare, falseTarget, trueTarget);
  }

  private static boolean hasSubloop(
      LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> loops) {
    return loops.stream().anyMatch(
        nested -> nested != loop
            && loop.contains(nested.header())
            && nested.blocks().size() < loop.blocks().size());
  }

  private record LoopExit(Instruction compare, BasicBlock body, BasicBlock exit) {}
}
