package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import java.util.ArrayList;
import java.util.List;

/** Threads predecessor-known boolean PHI values directly to branch destinations. */
final class ShortCircuitBranchThreading {
  private ShortCircuitBranchThreading() {}

  static boolean run(Function function) {
    boolean changed = false;
    for (BasicBlock block : new ArrayList<>(function.getBlocks())) {
      ShortCircuitThreadingPlan plan =
          ShortCircuitThreadingPlan.create(ShortCircuitBranch.match(block));
      if (plan == null) continue;
      apply(function, plan);
      changed = true;
    }
    return changed;
  }

  private static void apply(Function function, ShortCircuitThreadingPlan plan) {
    ShortCircuitBranch branch = plan.branch();
    PhiEdgeRewriter.replace(branch.ifTrue(), branch.block(), plan.truePredecessors());
    PhiEdgeRewriter.replace(branch.ifFalse(), branch.block(), plan.falsePredecessors());
    for (ShortCircuitThreadingPlan.Route route : plan.routes()) {
      Instruction terminator = route.predecessor().getTerminator();
      if (route.condition() == null) {
        redirect(terminator, branch.block(), route.target());
      } else {
        IRBuilder builder = new IRBuilder();
        builder.setInsertPointBefore(terminator);
        builder.createCondBr(route.condition(), branch.ifTrue(), branch.ifFalse());
        terminator.eraseFromParent();
      }
    }
    for (Instruction instruction : List.copyOf(branch.block().getInstructions())) {
      instruction.eraseFromParent();
    }
    function.removeBlock(branch.block());
  }

  private static void redirect(
      Instruction terminator, BasicBlock oldTarget, BasicBlock newTarget) {
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == oldTarget) terminator.setOperand(index, newTarget);
    }
  }
}
