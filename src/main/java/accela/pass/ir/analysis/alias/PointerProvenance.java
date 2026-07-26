package accela.pass.ir.analysis.alias;

import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Value;

/** Classifies pointers by the global, stack slot, or argument they originate from. */
public final class PointerProvenance {
  private PointerProvenance() {}

  public static Value root(Value pointer) {
    while (pointer instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.GEP) {
      pointer = instruction.getOperand(0);
    }
    return pointer;
  }

  /** Proves non-aliasing for distinct global objects and distinct stack objects. */
  public static boolean mayAlias(Value left, Value right) {
    if (left == right) return true;
    Value leftRoot = root(left);
    Value rightRoot = root(right);
    if (leftRoot == rightRoot) return true;
    if (leftRoot instanceof GlobalVariable && rightRoot instanceof GlobalVariable) return false;
    if (isAlloca(leftRoot) && isAlloca(rightRoot)) return false;
    if ((leftRoot instanceof GlobalVariable && isAlloca(rightRoot))
        || (rightRoot instanceof GlobalVariable && isAlloca(leftRoot))) return false;
    return true;
  }

  private static boolean isAlloca(Value value) {
    return value instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.ALLOCA;
  }
}
