package accela.pass.ir.transform;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;

/** Recognition helpers for loop-varying affine GEP addresses. */
final class AffineGepCandidate {
  private AffineGepCandidate() {}

  static int varyingIndex(
      Instruction gep, Instruction induction, LoopAnalysis.Loop loop) {
    for (int index = 1; index < gep.getNumOperands(); index++) {
      Value operand = gep.getOperand(index);
      if (isAffineIndex(operand, induction, loop)) return index;
    }
    return -1;
  }

  static boolean isAffineIndex(
      Value value, Instruction induction, LoopAnalysis.Loop loop) {
    if (value == induction) return true;
    if (!(value instanceof Instruction instruction)) return false;
    if (instruction.getOpcode() == Instruction.Opcode.SEXT) {
      return isAffineIndex(instruction.getOperand(0), induction, loop);
    }
    if (instruction.getNumOperands() != 2) return false;
    if (instruction.getOpcode() == Instruction.Opcode.ADD) {
      return isAffineIndex(instruction.getOperand(0), induction, loop)
              && isInvariant(instruction.getOperand(1), loop)
          || isAffineIndex(instruction.getOperand(1), induction, loop)
              && isInvariant(instruction.getOperand(0), loop);
    }
    return instruction.getOpcode() == Instruction.Opcode.SUB
        && isAffineIndex(instruction.getOperand(0), induction, loop)
        && isInvariant(instruction.getOperand(1), loop);
  }

  static boolean otherOperandsAreInvariant(
      Instruction gep, int varyingIndex, LoopAnalysis.Loop loop) {
    for (int index = 0; index < gep.getNumOperands(); index++) {
      if (index == varyingIndex) continue;
      if (!isInvariant(gep.getOperand(index), loop)) return false;
    }
    return true;
  }

  static boolean isInvariant(Value value, LoopAnalysis.Loop loop) {
    if (!(value instanceof Instruction definition)
        || !loop.blocks().contains(definition.getParent())) return true;
    if ((definition.getOpcode() == Instruction.Opcode.SEXT
        || definition.getOpcode() == Instruction.Opcode.ZEXT)
        && definition.getNumOperands() == 1) {
      return isInvariant(definition.getOperand(0), loop);
    }
    if (definition.getOpcode() == Instruction.Opcode.ADD
        || definition.getOpcode() == Instruction.Opcode.SUB
        || definition.getOpcode() == Instruction.Opcode.MUL) {
      for (int index = 0; index < definition.getNumOperands(); index++) {
        if (!isInvariant(definition.getOperand(index), loop)) return false;
      }
      return true;
    }
    if (definition.getOpcode() == Instruction.Opcode.GEP) {
      for (int index = 0; index < definition.getNumOperands(); index++) {
        if (!isInvariant(definition.getOperand(index), loop)) return false;
      }
      return true;
    }
    return false;
  }

  static boolean isMemoryAddress(Instruction gep) {
    return gep.getUses().stream().anyMatch(use -> {
      var opcode = use.getUser().getOpcode();
      return opcode == Instruction.Opcode.LOAD || opcode == Instruction.Opcode.STORE
          || opcode == Instruction.Opcode.GEP
              && isMemoryAddress((Instruction) use.getUser());
    });
  }

  static long strideAt(Instruction gep, int targetIndex) {
    Type type = gep.getGepSourceType();
    for (int index = 1; index < targetIndex; index++) if (type.isArray()) type = type.innerType;
    return sizeOf(type);
  }

  /** Returns {@code right - left} in bytes when both GEPs have one affine shape. */
  static Long byteOffsetDifference(Instruction left, Instruction right) {
    if (left.getOpcode() != Instruction.Opcode.GEP
        || right.getOpcode() != Instruction.Opcode.GEP
        || left.getNumOperands() != right.getNumOperands()
        || !sameType(left.getGepSourceType(), right.getGepSourceType())
        || !sameAddressExpression(left.getOperand(0), right.getOperand(0))) return null;
    long difference = 0;
    for (int index = 1; index < left.getNumOperands(); index++) {
      Long indexDifference = AffineValueForm.difference(
          left.getOperand(index), right.getOperand(index));
      if (indexDifference == null) return null;
      difference += indexDifference * strideAt(left, index);
    }
    return difference;
  }

  static boolean sameAddressExpression(Value first, Value second) {
    return sameAddressExpression(first, second, 0);
  }

  private static boolean sameAddressExpression(Value first, Value second, int depth) {
    if (first == second) return true;
    if (depth > 12) return false;
    if (first instanceof Constant.Int left && second instanceof Constant.Int right) {
      return left.value == right.value && sameType(left.getType(), right.getType());
    }
    if (!(first instanceof Instruction left) || !(second instanceof Instruction right)
        || left.getOpcode() != right.getOpcode()
        || !sameType(left.getType(), right.getType())
        || left.getNumOperands() != right.getNumOperands()) return false;
    switch (left.getOpcode()) {
      case ADD: case SUB: case SEXT: case ZEXT:
        break;
      case GEP:
        if (!sameType(left.getGepSourceType(), right.getGepSourceType())) return false;
        break;
      default:
        return false;
    }
    for (int index = 0; index < left.getNumOperands(); index++) {
      if (!sameAddressExpression(
          left.getOperand(index), right.getOperand(index), depth + 1)) return false;
    }
    return true;
  }

  private static boolean sameType(Type first, Type second) {
    if (first == second) return true;
    if (first == null || second == null || first.dataType != second.dataType
        || first.size != second.size) return false;
    return sameType(first.innerType, second.innerType);
  }

  private static long sizeOf(Type type) {
    if (type.isArray()) return type.size * sizeOf(type.innerType);
    return type == Type.I64 || type.isPointer() ? 8 : type == Type.I1 ? 1 : 4;
  }
}
