package accela.pass.ir.transform.indvars;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.InductionVariableAnalysis;

/** A unit-stride pointer PHI aligned with a canonical integer induction. */
record PointerInduction(Instruction phi, Value initial, Instruction next) {
  static PointerInduction find(InductionVariableAnalysis.Induction induction) {
    BasicBlock header = induction.loop().header();
    for (Instruction phi : header.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      if (phi.getType() != Type.PTR || phi.getNumOperands() != 4) continue;
      Value initial = incomingValue(phi, induction.predecessor());
      Value backedge = incomingValue(phi, induction.latch());
      if (initial == null
          || !(backedge instanceof Instruction next)
          || next.getParent() != induction.latch()
          || next.getOpcode() != Instruction.Opcode.GEP
          || next.getNumOperands() != 2
          || next.getGepSourceType().isArray()
          || next.getOperand(0) != phi
          || !(next.getOperand(1) instanceof Constant.Int step)
          || step.value != 1) continue;
      return new PointerInduction(phi, initial, next);
    }
    return null;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }
}
