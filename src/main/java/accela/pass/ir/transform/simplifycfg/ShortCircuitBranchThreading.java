package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
/** Threads boolean PHI values directly into their successor branches. */
final class ShortCircuitBranchThreading {
  private ShortCircuitBranchThreading() {}
  static boolean run(Function function) {
    long instructionCount = function.getBlocks().stream()
        .mapToLong(block -> block.getInstructions().size()).sum();
    boolean hasCall = function.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
    if (instructionCount > 500 || hasCall && instructionCount > 50) return false;
    boolean changed = false;
    for (BasicBlock merge : new ArrayList<>(function.getBlocks())) {
      Pattern pattern = match(merge);
      if (pattern == null || !canRewrite(pattern)) continue;
      ArrayList<Instruction> cleanup = new ArrayList<>();
      for (int index = 0; index < pattern.phi.getNumOperands(); index += 2) {
        Value value = pattern.phi.getOperand(index);
        BasicBlock predecessor = (BasicBlock) pattern.phi.getOperand(index + 1);
        Instruction terminator = predecessor.getTerminator();
        if (value instanceof Constant.Int bit) {
          redirect(terminator, merge, bit.value == 0 ? pattern.ifFalse : pattern.ifTrue);
        } else {
          Instruction extension = (Instruction) value;
          IRBuilder builder = new IRBuilder();
          builder.setInsertPointBefore(terminator);
          builder.createCondBr(extension.getOperand(0), pattern.ifTrue, pattern.ifFalse);
          terminator.eraseFromParent();
          cleanup.add(extension);
        }
      }
      for (Instruction instruction : new ArrayList<>(merge.getInstructions())) {
        instruction.eraseFromParent();
      }
      function.removeBlock(merge);
      for (Instruction instruction : cleanup) {
        if (!instruction.hasUses()) instruction.eraseFromParent();
      }
      changed = true;
    }
    return changed;
  }
  private static Pattern match(BasicBlock merge) {
    if (merge.getInstructions().size() != 3) return null;
    Instruction phi = merge.getInstructions().get(0);
    Instruction compare = merge.getInstructions().get(1);
    Instruction branch = merge.getInstructions().get(2);
    if (phi.getOpcode() != Instruction.Opcode.PHI || phi.getType() != Type.INT
        || phi.getNumOperands() < 2 || phi.getNumOperands() % 2 != 0
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || !"ne".equals(compare.getPredicate())
        || compare.getOperand(0) != phi
        || !(compare.getOperand(1) instanceof Constant.Int zero) || zero.value != 0
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || branch.getOperand(0) != compare) return null;
    BasicBlock ifTrue = (BasicBlock) branch.getOperand(1);
    BasicBlock ifFalse = (BasicBlock) branch.getOperand(2);
    if (startsWithPhi(ifTrue) || startsWithPhi(ifFalse)) return null;
    return new Pattern(phi, ifTrue, ifFalse, merge);
  }
  private static boolean canRewrite(Pattern pattern) {
    for (int index = 0; index < pattern.phi.getNumOperands(); index += 2) {
      Value value = pattern.phi.getOperand(index);
      BasicBlock predecessor = (BasicBlock) pattern.phi.getOperand(index + 1);
      Instruction terminator = predecessor.getTerminator();
      if (value instanceof Constant.Int bit) {
        if ((bit.value != 0 && bit.value != 1) || !targets(terminator, pattern.merge)) {
          return false;
        }
      } else if (!(value instanceof Instruction extension)
          || extension.getOpcode() != Instruction.Opcode.ZEXT
          || extension.getOperand(0).getType() != Type.I1
          || terminator.getOpcode() != Instruction.Opcode.BR
          || terminator.getOperand(0) != pattern.merge) {
        return false;
      }
    }
    return true;
  }
  private static boolean startsWithPhi(BasicBlock block) {
    return !block.getInstructions().isEmpty()
        && block.getInstructions().get(0).getOpcode() == Instruction.Opcode.PHI;
  }
  private static boolean targets(Instruction terminator, BasicBlock target) {
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == target) return true;
    }
    return false;
  }
  private static void redirect(
      Instruction terminator, BasicBlock oldTarget, BasicBlock newTarget) {
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == oldTarget) terminator.setOperand(index, newTarget);
    }
  }
  private record Pattern(
      Instruction phi, BasicBlock ifTrue, BasicBlock ifFalse, BasicBlock merge) {}
}
