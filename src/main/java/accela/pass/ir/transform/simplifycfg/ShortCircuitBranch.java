package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;

/** A branch on an i1 PHI whose value is known on each predecessor edge. */
record ShortCircuitBranch(
    BasicBlock block,
    Instruction phi,
    BasicBlock ifTrue,
    BasicBlock ifFalse) {

  static ShortCircuitBranch match(BasicBlock block) {
    if (block.getInstructions().size() != 2) return null;
    Instruction phi = block.getInstructions().getFirst();
    Instruction branch = block.getTerminator();
    if (phi.getOpcode() != Instruction.Opcode.PHI
        || phi.getType() != Type.I1
        || branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || branch.getOperand(0) != phi
        || branch.getOperand(1) == branch.getOperand(2)) return null;
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      Value incoming = phi.getOperand(index);
      if (incoming.getType() != Type.I1 || !isBoolean(incoming)) return null;
    }
    return new ShortCircuitBranch(
        block,
        phi,
        (BasicBlock) branch.getOperand(1),
        (BasicBlock) branch.getOperand(2));
  }

  private static boolean isBoolean(Value value) {
    return !(value instanceof Constant.Int integer)
        || integer.value == 0
        || integer.value == 1;
  }
}
