package accela.pass.ir.transform.looploadelimination;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;

/**
 * Forwards a store to a unit-distance load in the next loop iteration.
 *
 * <p>This is the conservative, no-runtime-alias-check subset of LLVM's
 * {@code LoopLoadElimination}.
 */
public final class LoopLoadElimination {
  private LoopLoadElimination() {}

  public static boolean canOptimize(LoopAnalysis.Loop loop) {
    return LoopLoadCandidate.match(loop) != null;
  }

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
      LoopLoadCandidate candidate = LoopLoadCandidate.match(loop);
      if (candidate == null || !candidate.isUnconditional(loop) || loop.preheader() == null) {
        continue;
      }
      rewrite(loop, candidate);
      changed = true;
    }
    return changed;
  }

  private static void rewrite(
      LoopAnalysis.Loop loop, LoopLoadCandidate candidate) {
    BasicBlock preheader = loop.preheader();
    IRBuilder preheaderBuilder = new IRBuilder();
    preheaderBuilder.setInsertPointBefore(preheader.getTerminator());
    Value initialPointer = candidate.initialPointer();
    if (candidate.loadOffset() != 0) {
      initialPointer = preheaderBuilder.createGEP(
          candidate.load().getType(),
          initialPointer,
          new Value[] {Constant.int64Const(candidate.loadOffset())},
          false);
    }
    Instruction initial =
        preheaderBuilder.createLoad(candidate.load().getType(), initialPointer);

    Instruction forwarded = Instruction.createPhi(candidate.load().getType());
    loop.header().addInstructionToFront(forwarded);
    forwarded.addOperand(initial);
    forwarded.addOperand(preheader);
    candidate.load().replaceAllUsesWith(forwarded);
    forwarded.addOperand(candidate.store().getOperand(0));
    forwarded.addOperand(loop.latches().iterator().next());
    candidate.load().eraseFromParent();
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopLoadElimination.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
