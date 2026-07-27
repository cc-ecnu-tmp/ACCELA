package accela.pass.ir.transform.indvars;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.InductionVariableAnalysis;

/** A guarded rotated {@code ++i < bound} exit whose integer IV can become dead. */
record RotatedExit(Instruction branch, Instruction compare, Value bound, long start) {
  static RotatedExit match(InductionVariableAnalysis.Induction induction) {
    if (induction.loop().header() != induction.latch()
        || induction.step() != 1
        || !(induction.start() instanceof Constant.Int start)
        || start.value < 0
        || start.value > Integer.MAX_VALUE) return null;
    Instruction branch = induction.latch().getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || branch.getOperand(1) != induction.loop().header()
        || induction.loop().contains((BasicBlock) branch.getOperand(2))
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || !"slt".equals(compare.getPredicate())
        || compare.getOperand(0) != induction.next()) return null;
    Value bound = compare.getOperand(1);
    if (!isInvariant(bound, induction)
        || !hasEntryGuard(induction, bound)
        || !integerInductionBecomesDead(induction, compare)) return null;
    return new RotatedExit(branch, compare, bound, start.value);
  }

  private static boolean hasEntryGuard(
      InductionVariableAnalysis.Induction induction, Value bound) {
    BasicBlock preheader = induction.predecessor();
    if (preheader.getPredecessors().size() != 1) return false;
    Instruction guard = preheader.getPredecessors().getFirst().getTerminator();
    if (guard == null
        || guard.getOpcode() != Instruction.Opcode.CONDBR
        || guard.getOperand(1) != preheader
        || !(guard.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || !"slt".equals(compare.getPredicate())
        || compare.getOperand(1) != bound) return false;
    Value guardedStart = compare.getOperand(0);
    if (sameValue(guardedStart, induction.start())) return true;
    return guardedStart instanceof Instruction phi
        && phi.getOpcode() == Instruction.Opcode.PHI
        && phi.getNumOperands() == 2
        && sameValue(phi.getOperand(0), induction.start());
  }

  private static boolean sameValue(Value left, Value right) {
    return left == right
        || left instanceof Constant.Int leftInt
            && right instanceof Constant.Int rightInt
            && leftInt.getType() == rightInt.getType()
            && leftInt.value == rightInt.value;
  }

  private static boolean integerInductionBecomesDead(
      InductionVariableAnalysis.Induction induction, Instruction compare) {
    return induction.next().getUses().stream().allMatch(
            use -> use.getUser() == induction.phi()
                || use.getUser() == compare
                || isDeadAddress(use.getUser()))
        && induction.phi().getUses().stream().allMatch(
            use -> use.getUser() == induction.next() || isDeadAddress(use.getUser()));
  }

  private static boolean isDeadAddress(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, GEP, SEXT, ZEXT ->
          instruction.getUses().stream().allMatch(use -> isDeadAddress(use.getUser()));
      default -> false;
    };
  }

  private static boolean isInvariant(
      Value value, InductionVariableAnalysis.Induction induction) {
    return !(value instanceof Instruction instruction)
        || !induction.loop().contains(instruction.getParent());
  }
}
