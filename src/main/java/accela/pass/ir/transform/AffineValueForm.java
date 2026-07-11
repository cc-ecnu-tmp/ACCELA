package accela.pass.ir.transform;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.Map;

/** Canonical linear form for integer SSA expressions. */
final class AffineValueForm {
  private final Map<Value, Long> coefficients = new IdentityHashMap<>();
  private long constant;

  static Long difference(Value left, Value right) {
    AffineValueForm lhs = build(left);
    AffineValueForm rhs = build(right);
    if (lhs.coefficients.size() != rhs.coefficients.size()) return null;
    for (var entry : lhs.coefficients.entrySet()) {
      if (!entry.getValue().equals(rhs.coefficients.get(entry.getKey()))) return null;
    }
    return rhs.constant - lhs.constant;
  }

  private static AffineValueForm build(Value value) {
    if (value instanceof Constant.Int integer) {
      AffineValueForm result = new AffineValueForm();
      result.constant = integer.value;
      return result;
    }
    if (!(value instanceof Instruction instruction)) return variable(value);
    return switch (instruction.getOpcode()) {
      case SEXT, ZEXT -> build(instruction.getOperand(0));
      case ADD -> combine(build(instruction.getOperand(0)), build(instruction.getOperand(1)), 1);
      case SUB -> combine(build(instruction.getOperand(0)), build(instruction.getOperand(1)), -1);
      case MUL -> multiply(instruction);
      default -> variable(value);
    };
  }

  private static AffineValueForm multiply(Instruction instruction) {
    if (instruction.getOperand(0) instanceof Constant.Int constant) {
      return scale(build(instruction.getOperand(1)), constant.value);
    }
    if (instruction.getOperand(1) instanceof Constant.Int constant) {
      return scale(build(instruction.getOperand(0)), constant.value);
    }
    return variable(instruction);
  }

  private static AffineValueForm variable(Value value) {
    AffineValueForm result = new AffineValueForm();
    result.coefficients.put(value, 1L);
    return result;
  }

  private static AffineValueForm combine(
      AffineValueForm left, AffineValueForm right, long rightScale) {
    AffineValueForm result = scale(left, 1);
    for (var entry : right.coefficients.entrySet()) {
      result.coefficients.merge(entry.getKey(), entry.getValue() * rightScale, Long::sum);
      if (result.coefficients.get(entry.getKey()) == 0) {
        result.coefficients.remove(entry.getKey());
      }
    }
    result.constant += right.constant * rightScale;
    return result;
  }

  private static AffineValueForm scale(AffineValueForm source, long factor) {
    AffineValueForm result = new AffineValueForm();
    for (var entry : source.coefficients.entrySet()) {
      result.coefficients.put(entry.getKey(), entry.getValue() * factor);
    }
    result.constant = source.constant * factor;
    return result;
  }
}
