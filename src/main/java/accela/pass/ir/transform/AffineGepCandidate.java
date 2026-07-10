package accela.pass.ir.transform;

import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;

/** Recognition helpers for loop-varying affine GEP addresses. */
final class AffineGepCandidate {
  private AffineGepCandidate() {}

  static int varyingIndex(Instruction gep, Instruction induction) {
    for (int index = 1; index < gep.getNumOperands(); index++) {
      Value operand = gep.getOperand(index);
      if (operand == induction || operand instanceof Instruction extension
          && extension.getOpcode() == Instruction.Opcode.SEXT
          && extension.getOperand(0) == induction) return index;
    }
    return -1;
  }

  static boolean otherOperandsAreInvariant(
      Instruction gep, int varyingIndex, LoopAnalysis.Loop loop) {
    for (int index = 0; index < gep.getNumOperands(); index++) {
      if (index == varyingIndex) continue;
      if (!isInvariant(gep.getOperand(index), loop)) return false;
    }
    return true;
  }

  private static boolean isInvariant(Value value, LoopAnalysis.Loop loop) {
    if (!(value instanceof Instruction definition)
        || !loop.blocks().contains(definition.getParent())) return true;
    if ((definition.getOpcode() == Instruction.Opcode.SEXT
        || definition.getOpcode() == Instruction.Opcode.ZEXT)
        && definition.getNumOperands() == 1) {
      return isInvariant(definition.getOperand(0), loop);
    }
    return false;
  }

  static boolean isMemoryAddress(Instruction gep) {
    return gep.getUses().stream().anyMatch(use -> {
      var opcode = use.getUser().getOpcode();
      return opcode == Instruction.Opcode.LOAD || opcode == Instruction.Opcode.STORE;
    });
  }

  static long strideAt(Instruction gep, int targetIndex) {
    Type type = gep.getGepSourceType();
    for (int index = 1; index < targetIndex; index++) if (type.isArray()) type = type.innerType;
    return sizeOf(type);
  }

  private static long sizeOf(Type type) {
    if (type.isArray()) return type.size * sizeOf(type.innerType);
    return type == Type.I64 || type.isPointer() ? 8 : type == Type.I1 ? 1 : 4;
  }
}
