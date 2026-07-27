package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import java.util.ArrayList;
import java.util.List;

/** Recognizes canonical integer PHIs with one constant-step backedge. */
public final class InductionVariableAnalysis
    implements FunctionAnalysis<InductionVariableAnalysis.Result> {
  public record Induction(
      LoopAnalysis.Loop loop,
      BasicBlock predecessor,
      BasicBlock latch,
      Instruction phi,
      Value start,
      Instruction next,
      long step) {}

  /** Separates transform-ready inductions from recurrences entered through another loop. */
  public record Result(List<Induction> inductions, List<Induction> allInductions) {
    public Result {
      inductions = List.copyOf(inductions);
      allInductions = List.copyOf(allInductions);
    }
  }

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    List<Induction> inductions = new ArrayList<>();
    List<Induction> allInductions = new ArrayList<>();
    List<LoopAnalysis.Loop> loops =
        fam.getResult(LoopAnalysis.class, function).loops();
    for (LoopAnalysis.Loop loop : loops) {
      BasicBlock predecessor = uniqueOutsidePredecessor(loop);
      if (predecessor == null
          || loop.latches().size() != 1) continue;
      boolean canonicalEntry = !belongsToUnrelatedLoop(predecessor, loop, loops);
      BasicBlock latch = loop.latches().iterator().next();
      for (Instruction phi : loop.header().getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value start = incomingValue(phi, predecessor);
        Value backedge = incomingValue(phi, latch);
        if (start == null
            || !(backedge instanceof Instruction next)
            || next.getParent() != latch) continue;
        Long step = constantStep(next, phi);
        if (step != null && step != 0) {
          Induction induction =
              new Induction(loop, predecessor, latch, phi, start, next, step);
          allInductions.add(induction);
          if (canonicalEntry) inductions.add(induction);
        }
      }
    }
    return new Result(inductions, allInductions);
  }

  private static boolean belongsToUnrelatedLoop(
      BasicBlock predecessor,
      LoopAnalysis.Loop target,
      List<LoopAnalysis.Loop> loops) {
    return loops.stream().anyMatch(
        loop -> loop.contains(predecessor) && !loop.contains(target.header()));
  }

  private static BasicBlock uniqueOutsidePredecessor(LoopAnalysis.Loop loop) {
    List<BasicBlock> predecessors = loop.header().getPredecessors().stream()
        .filter(block -> !loop.contains(block))
        .toList();
    return predecessors.size() == 1 ? predecessors.getFirst() : null;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index + 1 < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static Long constantStep(Instruction next, Instruction phi) {
    if (next.getOpcode() == Instruction.Opcode.ADD) {
      if (next.getOperand(0) == phi && next.getOperand(1) instanceof Constant.Int value) {
        return value.value;
      }
      if (next.getOperand(1) == phi && next.getOperand(0) instanceof Constant.Int value) {
        return value.value;
      }
    }
    if (next.getOpcode() == Instruction.Opcode.SUB
        && next.getOperand(0) == phi
        && next.getOperand(1) instanceof Constant.Int value) {
      return -value.value;
    }
    return null;
  }
}
