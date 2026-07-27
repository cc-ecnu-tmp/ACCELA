package accela.pass.ir.transform.loop.strength;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.Map;

/** A GEP address split into an identity base, symbolic byte terms, and a byte offset. */
final class AddressFormula {
  private final Value base;
  private final Map<Value, Long> terms = new IdentityHashMap<>();
  private long offset;

  private AddressFormula(Value base) {
    this.base = base;
  }

  static AddressFormula match(Instruction gep) {
    AddressFormula formula = new AddressFormula(gep.getOperand(0));
    try {
      for (int index = 1; index < gep.getNumOperands(); index++) {
        formula.add(gep.getOperand(index), AffineGep.byteStride(gep, index));
      }
      return formula;
    } catch (ArithmeticException overflow) {
      return null;
    }
  }

  /** Returns this-minus-base when both addresses have identical symbolic terms. */
  Long offsetFrom(AddressFormula other) {
    if (base != other.base || terms.size() != other.terms.size()) return null;
    for (var term : terms.entrySet()) {
      if (!term.getValue().equals(other.terms.get(term.getKey()))) return null;
    }
    try {
      return Math.subtractExact(offset, other.offset);
    } catch (ArithmeticException overflow) {
      return null;
    }
  }

  private void add(Value value, long scale) {
    if (value instanceof Constant.Int constant) {
      offset = Math.addExact(offset, Math.multiplyExact(scale, constant.value));
      return;
    }
    if (value instanceof Instruction instruction) {
      switch (instruction.getOpcode()) {
        case SEXT -> {
          add(instruction.getOperand(0), scale);
          return;
        }
        case ADD -> {
          add(instruction.getOperand(0), scale);
          add(instruction.getOperand(1), scale);
          return;
        }
        case SUB -> {
          add(instruction.getOperand(0), scale);
          add(instruction.getOperand(1), Math.negateExact(scale));
          return;
        }
        case MUL -> {
          if (addScaledProduct(instruction, scale)) return;
        }
        default -> {}
      }
    }
    long coefficient = Math.addExact(terms.getOrDefault(value, 0L), scale);
    if (coefficient == 0) terms.remove(value);
    else terms.put(value, coefficient);
  }

  private boolean addScaledProduct(Instruction multiply, long scale) {
    for (int constantIndex = 0; constantIndex < 2; constantIndex++) {
      if (multiply.getOperand(constantIndex) instanceof Constant.Int constant) {
        add(multiply.getOperand(1 - constantIndex),
            Math.multiplyExact(scale, constant.value));
        return true;
      }
    }
    return false;
  }
}
