package accela.utils.ir;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Utility helpers for local CFG rewrites that must keep PHI nodes in sync. */
public final class CFGUpdateUtils {
  private CFGUpdateUtils() {}

  /**
   * Removes the incoming edge from {@code predecessor} in one PHI.
   *
   * <p>If exactly one incoming pair remains afterwards, the PHI is folded away. If no incoming
   * pairs remain, the PHI is replaced with a typed zero value and erased. That zero is only a
   * structural placeholder for blocks that have just become unreachable; later CFG cleanup is
   * expected to remove the block itself.
   *
   * @return whether the PHI changed
   */
  public static boolean removePhiIncomingForPredecessor(Instruction phi, BasicBlock predecessor) {
    if (phi.getOpcode() != Instruction.Opcode.PHI) {
      throw new IllegalArgumentException("expected PHI instruction");
    }

    boolean removed = false;
    List<Value> newPairs = new ArrayList<>();
    for (int i = 0; i < phi.getNumOperands(); i += 2) {
      Value incomingValue = phi.getOperand(i);
      Value incomingBlock = phi.getOperand(i + 1);
      if (incomingBlock == predecessor) {
        removed = true;
        continue;
      }
      newPairs.add(incomingValue);
      newPairs.add(incomingBlock);
    }
    if (!removed) {
      return false;
    }
    if (newPairs.isEmpty()) {
      phi.replaceAllUsesWith(Constant.zero(phi.getType()));
      phi.eraseFromParent();
      return true;
    }
    if (newPairs.size() == 2) {
      phi.replaceAllUsesWith(newPairs.get(0));
      phi.eraseFromParent();
      return true;
    }

    phi.clearAllOperands();
    for (Value operand : newPairs) {
      phi.addOperand(operand);
    }
    return true;
  }

  /**
   * Removes the CFG edge {@code predecessor -> successor} from successor PHIs.
   *
   * <p>The caller is responsible for updating the actual terminator so this edge is no longer
   * present in the CFG.
   */
  public static boolean removePredecessorEdge(BasicBlock predecessor, BasicBlock successor) {
    boolean changed = false;
    for (Instruction inst : new ArrayList<>(successor.getInstructions())) {
      if (inst.getOpcode() != Instruction.Opcode.PHI) {
        break;
      }
      changed |= removePhiIncomingForPredecessor(inst, predecessor);
    }
    return changed;
  }

  /**
   * Rewrites a conditional branch into an unconditional branch to {@code target} and removes PHI
   * incoming values on successors whose edge is no longer present.
   */
  public static boolean rewriteCondBrToBr(BasicBlock block, BasicBlock target) {
    Instruction term = block.getTerminator();
    if (term == null || term.getOpcode() != Instruction.Opcode.CONDBR) {
      throw new IllegalArgumentException("block terminator is not condbr");
    }

    BasicBlock trueTarget = (BasicBlock) term.getOperand(1);
    BasicBlock falseTarget = (BasicBlock) term.getOperand(2);
    if (target != trueTarget && target != falseTarget) {
      throw new IllegalArgumentException("target is not a condbr successor");
    }

    Set<BasicBlock> removedSuccessors = new LinkedHashSet<>();
    if (trueTarget != target) {
      removedSuccessors.add(trueTarget);
    }
    if (falseTarget != target) {
      removedSuccessors.add(falseTarget);
    }
    for (BasicBlock removedSuccessor : removedSuccessors) {
      removePredecessorEdge(block, removedSuccessor);
    }

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(term);
    builder.createBr(target);
    term.eraseFromParent();
    return true;
  }
}
