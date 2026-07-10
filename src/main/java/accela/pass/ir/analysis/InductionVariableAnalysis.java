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

/** Recognizes canonical constant-step integer add recurrences. */
public final class InductionVariableAnalysis
    implements FunctionAnalysis<InductionVariableAnalysis.Result> {
  public record Induction(
      LoopAnalysis.Loop loop,
      Instruction phi,
      Value start,
      Instruction next,
      long step) {}

  public record Result(List<Induction> inductions) {}

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    List<Induction> inductions = new ArrayList<>();
    for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
      if (loop.preheader() == null) continue;
      for (Instruction phi : loop.header().getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value start = incomingValue(phi, loop.preheader());
        Value backedge = incomingValue(phi, loop.latch());
        if (start == null || !(backedge instanceof Instruction next)
            || next.getParent() != loop.latch()) continue;
        Long step = constantStep(next, phi);
        if (step != null && step != 0) {
          inductions.add(new Induction(loop, phi, start, next, step));
        }
      }
    }
    return new Result(List.copyOf(inductions));
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
        && next.getOperand(1) instanceof Constant.Int value) return -value.value;
    return null;
  }
}
