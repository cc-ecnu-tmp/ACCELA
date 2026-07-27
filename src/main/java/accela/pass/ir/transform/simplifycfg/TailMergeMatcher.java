package accela.pass.ir.transform.simplifycfg;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.Objects;

/** Structural matching shared by the narrow SimplifyCFG tail merger. */
final class TailMergeMatcher {
  private TailMergeMatcher() {}

  static boolean equivalent(Instruction left, Instruction right) {
    if (left.getOpcode() != right.getOpcode()
        || !left.getType().toString().equals(right.getType().toString())
        || !Objects.equals(left.getPredicate(), right.getPredicate())
        || left.getOpcode() == Instruction.Opcode.GEP
            && (!left.getGepSourceType().toString().equals(right.getGepSourceType().toString())
                || left.isGepInbounds() != right.isGepInbounds())
        || left.getNumOperands() != right.getNumOperands()) return false;
    for (int index = 0; index < left.getNumOperands(); index++) {
      if (!sameValue(left.getOperand(index), right.getOperand(index))) return false;
    }
    return true;
  }

  static boolean isPure(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SHL, ASHR, AND, FADD, FSUB, FMUL, FDIV, FNEG,
          ICMP, FCMP, GEP, ZEXT, SEXT, SITOFP, FPTOSI, XOR -> true;
      default -> false;
    };
  }

  static boolean sameValue(Value left, Value right) {
    if (left == right) return true;
    return left instanceof Constant && right instanceof Constant
        && left.getType() == right.getType()
        && Objects.equals(left.getName(), right.getName());
  }
}
