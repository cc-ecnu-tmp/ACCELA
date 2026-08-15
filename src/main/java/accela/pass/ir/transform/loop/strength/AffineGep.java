package accela.pass.ir.transform.loop.strength;

import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Recognizes GEP indices that vary linearly with one induction variable. */
final class AffineGep {
  private AffineGep() {}

  static int varyingIndex(
      Instruction gep, Instruction induction, LoopAnalysis.Loop loop) {
    int varying = -1;
    for (int index = 1; index < gep.getNumOperands(); index++) {
      Value operand = gep.getOperand(index);
      if (isAffine(operand, induction, loop)) {
        if (varying >= 0) return -1;
        varying = index;
      } else if (!isInvariant(operand, loop)) {
        return -1;
      }
    }
    return varying;
  }

  static boolean isMemoryAddress(Instruction gep) {
    return reachesMemory(gep, Collections.newSetFromMap(new IdentityHashMap<>()));
  }

  static boolean isDirectMemoryAddress(Instruction gep) {
    return gep.getUses().stream().anyMatch(use -> {
      Instruction user = use.getUser();
      return user.getOpcode() == Instruction.Opcode.LOAD
          || user.getOpcode() == Instruction.Opcode.STORE
          || user.getOpcode() == Instruction.Opcode.GEP && isDirectMemoryAddress(user);
    });
  }

  private static boolean reachesMemory(Value value, Set<Value> visited) {
    if (!visited.add(value)) return false;
    return value.getUses().stream().anyMatch(use -> {
      Instruction user = use.getUser();
      return user.getOpcode() == Instruction.Opcode.LOAD
          || user.getOpcode() == Instruction.Opcode.STORE
          || user.getOpcode() == Instruction.Opcode.GEP && reachesMemory(user, visited)
          || user.getOpcode() == Instruction.Opcode.PHI
              && pointerPhiCount(user) <= 2 && reachesMemory(user, visited);
    });
  }

  private static long pointerPhiCount(Instruction phi) {
    return phi.getParent().getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .filter(instruction -> instruction.getType() == Type.PTR)
        .count();
  }

  static long byteStride(Instruction gep, int varyingIndex) {
    Type type = gep.getGepSourceType();
    for (int index = 1; index < varyingIndex; index++) {
      if (type.isArray()) type = type.innerType;
    }
    return sizeOf(type);
  }

  static boolean isAffine(
      Value value, Instruction induction, LoopAnalysis.Loop loop) {
    if (value == induction) return true;
    if (!(value instanceof Instruction instruction)) return false;
    if (isExtension(instruction)) {
      return isAffine(instruction.getOperand(0), induction, loop);
    }
    if (instruction.getNumOperands() != 2) return false;
    if (instruction.getOpcode() == Instruction.Opcode.ADD) {
      return isAffine(instruction.getOperand(0), induction, loop)
              && isInvariant(instruction.getOperand(1), loop)
          || isAffine(instruction.getOperand(1), induction, loop)
              && isInvariant(instruction.getOperand(0), loop);
    }
    return instruction.getOpcode() == Instruction.Opcode.SUB
        && isAffine(instruction.getOperand(0), induction, loop)
        && isInvariant(instruction.getOperand(1), loop);
  }

  static boolean isInvariant(Value value, LoopAnalysis.Loop loop) {
    if (!(value instanceof Instruction instruction)
        || !loop.contains(instruction.getParent())) return true;
    if (isExtension(instruction)) {
      return isInvariant(instruction.getOperand(0), loop);
    }
    if (instruction.getOpcode() == Instruction.Opcode.GEP
        || instruction.getOpcode() == Instruction.Opcode.ADD
        || instruction.getOpcode() == Instruction.Opcode.SUB
        || instruction.getOpcode() == Instruction.Opcode.MUL) {
      for (int index = 0; index < instruction.getNumOperands(); index++) {
        if (!isInvariant(instruction.getOperand(index), loop)) return false;
      }
      return true;
    }
    return false;
  }

  private static boolean isExtension(Instruction instruction) {
    return instruction.getOpcode() == Instruction.Opcode.SEXT
        || instruction.getOpcode() == Instruction.Opcode.ZEXT;
  }

  private static long sizeOf(Type type) {
    if (type.isArray() || type.isVector()) return type.size * sizeOf(type.innerType);
    if (type == Type.I64 || type.isPointer()) return 8;
    return type == Type.I1 ? 1 : 4;
  }
}
