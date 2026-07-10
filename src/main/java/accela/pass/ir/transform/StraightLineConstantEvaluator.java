package accela.pass.ir.transform;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Evaluates side-effect-free single-block integer functions. */
final class StraightLineConstantEvaluator {
  private StraightLineConstantEvaluator() {}

  static Constant.Int evaluate(Function function, List<Constant.Int> arguments) {
    if (function.getReturnType() != Type.INT
        || function.getBlocks().size() != 1
        || function.getNumArgs() != arguments.size()) {
      return null;
    }

    Map<Value, Constant.Int> values = new IdentityHashMap<>();
    for (int i = 0; i < arguments.size(); i++) {
      if (function.getArguments().get(i).getType() != Type.INT) return null;
      values.put(function.getArguments().get(i), arguments.get(i));
    }

    for (Instruction instruction : function.getBlocks().get(0).getInstructions()) {
      if (instruction.getOpcode() == Instruction.Opcode.RET) {
        return valueOf(instruction.getOperand(0), values);
      }
      if (!isSupported(instruction.getOpcode()) || instruction.getType() != Type.INT) return null;
      Constant.Int left = valueOf(instruction.getOperand(0), values);
      Constant.Int right = valueOf(instruction.getOperand(1), values);
      if (left == null || right == null) return null;
      int lhs = (int) left.value;
      int rhs = (int) right.value;
      if ((instruction.getOpcode() == Instruction.Opcode.SDIV
          || instruction.getOpcode() == Instruction.Opcode.SREM) && rhs == 0) return null;
      int result = switch (instruction.getOpcode()) {
        case ADD -> lhs + rhs;
        case SUB -> lhs - rhs;
        case MUL -> lhs * rhs;
        case SDIV -> lhs / rhs;
        case SREM -> lhs % rhs;
        case XOR -> lhs ^ rhs;
        case AND -> lhs & rhs;
        default -> throw new IllegalStateException();
      };
      values.put(instruction, Constant.intConst(result));
    }
    return null;
  }

  private static Constant.Int valueOf(Value value, Map<Value, Constant.Int> values) {
    return value instanceof Constant.Int constant ? constant : values.get(value);
  }

  private static boolean isSupported(Instruction.Opcode opcode) {
    return opcode == Instruction.Opcode.ADD
        || opcode == Instruction.Opcode.SUB
        || opcode == Instruction.Opcode.MUL
        || opcode == Instruction.Opcode.SDIV
        || opcode == Instruction.Opcode.SREM
        || opcode == Instruction.Opcode.XOR
        || opcode == Instruction.Opcode.AND;
  }
}
