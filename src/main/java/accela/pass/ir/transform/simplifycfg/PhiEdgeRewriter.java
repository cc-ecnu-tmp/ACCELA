package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.List;

/** Updates successor PHIs when one predecessor is replaced by several direct edges. */
final class PhiEdgeRewriter {
  private PhiEdgeRewriter() {}

  static boolean canReplace(
      BasicBlock successor, BasicBlock removed, List<BasicBlock> replacements) {
    for (Instruction phi : successor.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      int removedIndex = incomingIndex(phi, removed);
      if (removedIndex < 0) return false;
      Value value = phi.getOperand(removedIndex);
      if (value instanceof Instruction instruction
          && instruction.getParent() == removed) return false;
      for (BasicBlock replacement : replacements) {
        if (incomingIndex(phi, replacement) >= 0) return false;
      }
    }
    return true;
  }

  static void replace(
      BasicBlock successor, BasicBlock removed, List<BasicBlock> replacements) {
    for (Instruction phi : new ArrayList<>(successor.getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      int removedIndex = incomingIndex(phi, removed);
      Value value = phi.getOperand(removedIndex);
      List<Value> operands = new ArrayList<>();
      for (int index = 0; index < phi.getNumOperands(); index += 2) {
        if (index == removedIndex) continue;
        operands.add(phi.getOperand(index));
        operands.add(phi.getOperand(index + 1));
      }
      for (BasicBlock replacement : replacements) {
        operands.add(value);
        operands.add(replacement);
      }
      phi.clearAllOperands();
      operands.forEach(phi::addOperand);
    }
  }

  private static int incomingIndex(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return index;
    }
    return -1;
  }
}
