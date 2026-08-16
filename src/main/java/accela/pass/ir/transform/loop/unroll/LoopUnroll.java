package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.List;
import java.util.Map;

/** Fully unrolls one small, constant-trip, innermost loop per invocation. */
public final class LoopUnroll {
  private LoopUnroll() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    LoopUnrollCandidate candidate = LoopUnrollCandidate.find(function, fam);
    if (candidate == null) return LoopPartialUnroll.run(function, fam);

    UnrolledLoopCloner.Result clones =
        UnrolledLoopCloner.clone(function, candidate);
    retargetEntry(candidate, clones.entry());
    replaceLiveOuts(candidate, clones.finalHeaderValues());
    eraseOriginalLoop(function, candidate);
    LoopUnrollCleanup.run(function);
    return true;
  }

  private static void retargetEntry(
      LoopUnrollCandidate candidate, BasicBlock entry) {
    Instruction terminator = candidate.induction().predecessor().getTerminator();
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == candidate.loop().header()) {
        terminator.setOperand(index, entry);
      }
    }
  }

  private static void replaceLiveOuts(
      LoopUnrollCandidate candidate,
      Map<Instruction, Value> finalHeaderValues) {
    for (var entry : finalHeaderValues.entrySet()) {
      Instruction phi = entry.getKey();
      for (var use : List.copyOf(phi.getUses())) {
        if (!candidate.loop().contains(use.getUser().getParent())) {
          use.getUser().setOperand(use.getOperandIndex(), entry.getValue());
        }
      }
    }
  }

  private static void eraseOriginalLoop(
      Function function, LoopUnrollCandidate candidate) {
    List<BasicBlock> blocks = function.getBlocks().stream()
        .filter(candidate.loop()::contains)
        .toList();
    for (BasicBlock block : blocks) {
      for (Instruction instruction : List.copyOf(block.getInstructions())) {
        instruction.eraseFromParent();
      }
    }
    for (BasicBlock block : blocks) function.removeBlock(block);
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopUnroll.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
