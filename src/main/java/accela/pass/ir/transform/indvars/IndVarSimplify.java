package accela.pass.ir.transform.indvars;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import java.util.ArrayList;
import java.util.List;

/** Simplifies comparisons implied by canonical induction-variable ranges. */
public final class IndVarSimplify {
  private IndVarSimplify() {}

  public static boolean runLFTR(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (var induction :
        fam.getResult(InductionVariableAnalysis.class, function).inductions()) {
      changed |= PointerLFTR.rewrite(induction);
    }
    return changed;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      List<IndVarRange> ranges = fam
          .getResult(InductionVariableAnalysis.class, function)
          .allInductions().stream()
          .map(IndVarRange::from)
          .filter(java.util.Objects::nonNull)
          .toList();
      DominatorTreeAnalysis.Result dominators =
          fam.getResult(DominatorTreeAnalysis.class, function);
      boolean changed = false;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
          if (instruction.getOpcode() == Instruction.Opcode.ICMP
              && ranges.stream().anyMatch(range -> range.provesTrue(instruction, dominators))) {
            instruction.replaceAllUsesWith(Constant.boolConst(true));
            instruction.eraseFromParent();
            changed = true;
          } else if (foldNonNegativeRemainder(instruction, ranges, dominators)) {
            changed = true;
          }
        }
      }
      return changed ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  private static boolean foldNonNegativeRemainder(
      Instruction remainder,
      List<IndVarRange> ranges,
      DominatorTreeAnalysis.Result dominators) {
    if (remainder.getOpcode() != Instruction.Opcode.SREM
        || !(remainder.getOperand(1) instanceof Constant.Int constant)) return false;
    int divisor = (int) constant.value;
    if (divisor <= 0
        || (divisor & (divisor - 1)) != 0
        || ranges.stream()
            .noneMatch(
                range -> range.provesNonNegative(remainder.getOperand(0), remainder, dominators))) {
      return false;
    }
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(remainder);
    var masked = builder.createAnd(remainder.getOperand(0), Constant.intConst(divisor - 1));
    remainder.replaceAllUsesWith(masked);
    remainder.eraseFromParent();
    return true;
  }

  public static final class LFTRPass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runLFTR(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
