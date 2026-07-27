package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Merges equivalent one-instruction blocks that branch to the same successor. */
final class TailMerge {
  private TailMerge() {}

  static boolean run(Function function) {
    Map<BasicBlock, List<BasicBlock>> candidates = new IdentityHashMap<>();
    for (BasicBlock block : function.getBlocks()) {
      BasicBlock successor = candidateSuccessor(block);
      if (successor != null) {
        candidates.computeIfAbsent(successor, ignored -> new ArrayList<>()).add(block);
      }
    }

    boolean changed = false;
    for (List<BasicBlock> blocks : candidates.values()) {
      for (int leftIndex = 0; leftIndex < blocks.size(); leftIndex++) {
        BasicBlock left = blocks.get(leftIndex);
        if (left.getParent() != function) continue;
        for (int rightIndex = leftIndex + 1; rightIndex < blocks.size(); rightIndex++) {
          BasicBlock right = blocks.get(rightIndex);
          if (right.getParent() != function || !canMerge(left, right)) continue;
          merge(function, left, right);
          changed = true;
        }
      }
    }
    return changed;
  }

  private static BasicBlock candidateSuccessor(BasicBlock block) {
    if (block.getInstructions().size() != 2) return null;
    Instruction value = block.getInstructions().getFirst();
    Instruction branch = block.getTerminator();
    return TailMergeMatcher.isPure(value)
            && branch != null
            && branch.getOpcode() == Instruction.Opcode.BR
        ? (BasicBlock) branch.getOperand(0)
        : null;
  }

  private static boolean canMerge(BasicBlock left, BasicBlock right) {
    if (left.getInstructions().size() != 2
        || right.getInstructions().size() != 2
        || right.getPredecessors().isEmpty()) return false;
    Instruction leftValue = left.getInstructions().getFirst();
    Instruction rightValue = right.getInstructions().getFirst();
    Instruction leftBranch = left.getTerminator();
    Instruction rightBranch = right.getTerminator();
    if (!TailMergeMatcher.isPure(leftValue)
        || leftBranch.getOpcode() != Instruction.Opcode.BR
        || rightBranch.getOpcode() != Instruction.Opcode.BR
        || leftBranch.getOperand(0) != rightBranch.getOperand(0)
        || !TailMergeMatcher.equivalent(leftValue, rightValue)) return false;
    return compatiblePhis(
        (BasicBlock) leftBranch.getOperand(0), left, right, leftValue, rightValue);
  }
  private static boolean compatiblePhis(
      BasicBlock successor,
      BasicBlock left,
      BasicBlock right,
      Instruction leftValue,
      Instruction rightValue) {
    for (Instruction phi : successor.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value leftIncoming = incomingValue(phi, left);
      Value rightIncoming = incomingValue(phi, right);
      if (leftIncoming == null || rightIncoming == null) return false;
      if (rightIncoming == rightValue) rightIncoming = leftValue;
      if (!TailMergeMatcher.sameValue(leftIncoming, rightIncoming)) return false;
    }
    return true;
  }
  private static void merge(Function function, BasicBlock kept, BasicBlock removed) {
    Instruction keptValue = kept.getInstructions().getFirst();
    Instruction removedValue = removed.getInstructions().getFirst();
    BasicBlock successor = (BasicBlock) removed.getTerminator().getOperand(0);
    for (BasicBlock predecessor : List.copyOf(removed.getPredecessors())) {
      Instruction branch = predecessor.getTerminator();
      for (int index = 0; index < branch.getNumOperands(); index++) {
        if (branch.getOperand(index) == removed) branch.setOperand(index, kept);
      }
    }
    removedValue.replaceAllUsesWith(keptValue);
    for (Instruction phi : successor.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      removeIncoming(phi, removed);
    }
    for (Instruction instruction : new ArrayList<>(removed.getInstructions())) {
      instruction.eraseFromParent();
    }
    function.removeBlock(removed);
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static void removeIncoming(Instruction phi, BasicBlock predecessor) {
    List<Value> operands = new ArrayList<>();
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) continue;
      operands.add(phi.getOperand(index));
      operands.add(phi.getOperand(index + 1));
    }
    phi.clearAllOperands();
    for (Value operand : operands) phi.addOperand(operand);
  }
}
