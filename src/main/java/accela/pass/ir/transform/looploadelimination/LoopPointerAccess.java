package accela.pass.ir.transform.looploadelimination;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;

/** A constant-offset access based on a unit-stride pointer recurrence. */
record LoopPointerAccess(Instruction pointer, Value initial, long offset, long step) {
  static LoopPointerAccess match(Value address, LoopAnalysis.Loop loop) {
    long offset = 0;
    if (address instanceof Instruction gep && isScalarOffset(gep)) {
      offset = ((Constant.Int) gep.getOperand(1)).value;
      address = gep.getOperand(0);
    }
    if (!(address instanceof Instruction phi)
        || phi.getOpcode() != Instruction.Opcode.PHI
        || phi.getType() != Type.PTR
        || phi.getParent() != loop.header()
        || phi.getNumOperands() != 4) return null;

    for (int index = 0; index < 4; index += 2) {
      BasicBlock predecessor = (BasicBlock) phi.getOperand(index + 1);
      if (!loop.contains(predecessor)) continue;
      Value next = phi.getOperand(index);
      if (!(next instanceof Instruction gep)
          || !isScalarOffset(gep)
          || gep.getOperand(0) != phi) return null;
      long step = ((Constant.Int) gep.getOperand(1)).value;
      if (step != 1 && step != -1) return null;
      return new LoopPointerAccess(phi, phi.getOperand(2 - index), offset, step);
    }
    return null;
  }

  private static boolean isScalarOffset(Instruction gep) {
    return gep.getOpcode() == Instruction.Opcode.GEP
        && gep.getNumOperands() == 2
        && !gep.getGepSourceType().isArray()
        && gep.getOperand(1) instanceof Constant.Int;
  }
}
