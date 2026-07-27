package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;

/** A canonical loop whose signed i32 trip count is known exactly. */
public record ExactTripCount(
    Instruction compare, BasicBlock body, BasicBlock exit, int count) {
  public static ExactTripCount find(InductionVariableAnalysis.Induction induction) {
    if (!(induction.start() instanceof Constant.Int start)
        || induction.step() <= 0
        || induction.step() > Integer.MAX_VALUE) return null;

    Instruction branch = induction.loop().header().getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || !"slt".equals(compare.getPredicate())
        || compare.getOperand(0) != induction.phi()
        || !(compare.getOperand(1) instanceof Constant.Int bound)) return null;

    BasicBlock body = (BasicBlock) branch.getOperand(1);
    BasicBlock exit = (BasicBlock) branch.getOperand(2);
    if (!induction.loop().contains(body) || induction.loop().contains(exit)) return null;

    long first = (int) start.value;
    long limit = (int) bound.value;
    long step = induction.step();
    if (first >= limit) return null;
    long count = (limit - first + step - 1) / step;
    long after = first + count * step;
    if (count > Integer.MAX_VALUE || after > Integer.MAX_VALUE) return null;
    return new ExactTripCount(compare, body, exit, (int) count);
  }
}
