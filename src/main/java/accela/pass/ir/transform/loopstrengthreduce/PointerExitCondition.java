package accela.pass.ir.transform.loopstrengthreduce;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.InductionVariableAnalysis;

/** Replaces a rotated integer loop test with an equivalent pointer end test. */
final class PointerExitCondition {
  private PointerExitCondition() {}

  static boolean rewrite(
      InductionVariableAnalysis.Induction induction,
      PointerRecurrence.Result recurrence) {
    if (recurrence.elementStep() != 1
        || !(induction.start() instanceof Constant.Int start) || start.value != 0
        || induction.step() != 1) return false;
    return induction.loop().header() == induction.latch()
        ? rewriteRotated(induction, recurrence)
        : rewritePretest(induction, recurrence);
  }

  private static boolean rewriteRotated(
      InductionVariableAnalysis.Induction induction,
      PointerRecurrence.Result recurrence) {
    Instruction branch = induction.latch().getTerminator();
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR
        || branch.getOperand(1) != induction.loop().header()
        || !(branch.getOperand(0) instanceof Instruction compare)
        || !isLessThan(compare, induction.next(), induction)
        || !integerInductionBecomesDead(induction, compare)) return false;
    replaceTest(induction, recurrence, branch, compare, recurrence.next());
    return true;
  }

  private static boolean rewritePretest(
      InductionVariableAnalysis.Induction induction,
      PointerRecurrence.Result recurrence) {
    Instruction branch = induction.loop().header().getTerminator();
    Instruction latchBranch = induction.latch().getTerminator();
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR
        || latchBranch == null || latchBranch.getOpcode() != Instruction.Opcode.BR
        || latchBranch.getOperand(0) != induction.loop().header()
        || !induction.loop().contains((BasicBlock) branch.getOperand(1))
        || induction.loop().contains((BasicBlock) branch.getOperand(2))
        || !(branch.getOperand(0) instanceof Instruction compare)
        || !isLessThan(compare, induction.phi(), induction)
        || !(compare.getOperand(1) instanceof Constant.Int bound) || bound.value < 0
        || !integerInductionBecomesDead(induction, compare)) return false;
    replaceTest(induction, recurrence, branch, compare, recurrence.pointer());
    return true;
  }

  private static void replaceTest(
      InductionVariableAnalysis.Induction induction,
      PointerRecurrence.Result recurrence,
      Instruction branch,
      Instruction compare,
      Value pointer) {
    Value bound = compare.getOperand(1);
    IRBuilder entryBuilder = new IRBuilder();
    entryBuilder.setInsertPointBefore(induction.predecessor().getTerminator());
    Value extendedBound = bound instanceof Constant.Int constant
        ? Constant.int64Const((int) constant.value)
        : entryBuilder.createSExt(bound, Type.I64);
    Instruction end = entryBuilder.createGEP(
        Type.INT, recurrence.initial(), new Value[] {extendedBound}, false);

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(branch);
    branch.setOperand(0, builder.createICmp("ne", pointer, end));
  }

  private static boolean isLessThan(
      Instruction compare,
      Value inductionValue,
      InductionVariableAnalysis.Induction induction) {
    return compare.getOpcode() == Instruction.Opcode.ICMP
        && "slt".equals(compare.getPredicate())
        && compare.getOperand(0) == inductionValue
        && isInvariant(compare.getOperand(1), induction);
  }

  private static boolean integerInductionBecomesDead(
      InductionVariableAnalysis.Induction induction, Instruction compare) {
    return induction.next().getUses().stream().allMatch(use ->
            use.getUser() == induction.phi() || use.getUser() == compare)
        && induction.phi().getUses().stream().allMatch(use ->
            use.getUser() == induction.next()
                || use.getUser() == compare
                || isDeadAddress(use.getUser()));
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
