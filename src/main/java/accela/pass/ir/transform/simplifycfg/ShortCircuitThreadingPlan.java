package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;

/** Validated predecessor routes for one short-circuit branch rewrite. */
record ShortCircuitThreadingPlan(
    ShortCircuitBranch branch,
    List<Route> routes,
    List<BasicBlock> truePredecessors,
    List<BasicBlock> falsePredecessors) {

  static ShortCircuitThreadingPlan create(ShortCircuitBranch branch) {
    if (branch == null) return null;
    List<Route> routes = new ArrayList<>();
    LinkedHashSet<BasicBlock> truePredecessors = new LinkedHashSet<>();
    LinkedHashSet<BasicBlock> falsePredecessors = new LinkedHashSet<>();
    for (int index = 0; index < branch.phi().getNumOperands(); index += 2) {
      Value value = branch.phi().getOperand(index);
      BasicBlock predecessor = (BasicBlock) branch.phi().getOperand(index + 1);
      Instruction terminator = predecessor.getTerminator();
      if (value instanceof Constant.Int bit) {
        BasicBlock target = bit.value == 0 ? branch.ifFalse() : branch.ifTrue();
        if (!targets(predecessor, branch.block())
            || hasOtherEdge(predecessor, target, branch.block())) return null;
        routes.add(new Route(predecessor, null, target));
        (bit.value == 0 ? falsePredecessors : truePredecessors).add(predecessor);
      } else {
        if (terminator == null
            || terminator.getOpcode() != Instruction.Opcode.BR
            || terminator.getOperand(0) != branch.block()
            || value instanceof Instruction instruction
                && instruction.getParent() != predecessor) return null;
        routes.add(new Route(predecessor, value, null));
        truePredecessors.add(predecessor);
        falsePredecessors.add(predecessor);
      }
    }
    List<BasicBlock> trueList = List.copyOf(truePredecessors);
    List<BasicBlock> falseList = List.copyOf(falsePredecessors);
    if (!PhiEdgeRewriter.canReplace(branch.ifTrue(), branch.block(), trueList)
        || !PhiEdgeRewriter.canReplace(branch.ifFalse(), branch.block(), falseList)) return null;
    return new ShortCircuitThreadingPlan(branch, routes, trueList, falseList);
  }

  private static boolean targets(BasicBlock predecessor, BasicBlock target) {
    return predecessor.getSuccessors().contains(target);
  }

  private static boolean hasOtherEdge(
      BasicBlock predecessor, BasicBlock target, BasicBlock replaced) {
    return predecessor.getSuccessors().stream()
        .anyMatch(successor -> successor == target && successor != replaced);
  }

  record Route(BasicBlock predecessor, Value condition, BasicBlock target) {}
}
