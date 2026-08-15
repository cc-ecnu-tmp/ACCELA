package accela.pass.ir.transform.gvn;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Value-numbers side-effect-free instructions within the current dominator scope. */
final class GVNValueTable {
  private final Map<Expression, Value> available = new HashMap<>();
  private final GVNMemoryTable memory;

  private GVNValueTable(
      Map<Expression, Value> available, GVNMemoryTable memory) {
    this.available.putAll(available);
    this.memory = memory;
  }

  GVNValueTable() {
    memory = new GVNMemoryTable();
  }

  GVNValueTable copy() {
    return new GVNValueTable(available, memory.copy());
  }

  Value findOrAdd(
      Instruction instruction,
      List<Expression> introduced,
      GlobalModRefAnalysis.Result modRef) {
    if (instruction.getOpcode() == Instruction.Opcode.CALL
        && modRef != null && modRef.isPure(instruction)) {
      Expression expression = expressionFor(instruction);
      Value existing = available.putIfAbsent(expression, instruction);
      if (existing == null) introduced.add(expression);
      return existing;
    }
    if (instruction.getOpcode() == Instruction.Opcode.LOAD
        || instruction.getOpcode() == Instruction.Opcode.STORE
        || instruction.getOpcode() == Instruction.Opcode.CALL) {
      return memory.findOrAdd(instruction, modRef);
    }
    if (!isPure(instruction)) return null;
    Expression expression = expressionFor(instruction);
    Value existing = available.putIfAbsent(expression, instruction);
    if (existing == null) introduced.add(expression);
    return existing;
  }

  void leaveScope(List<Expression> introduced) {
    introduced.forEach(available::remove);
  }

  static boolean isPure(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR,
          FADD, FSUB, FMUL, FDIV, FNEG, ICMP, FCMP, GEP,
          ZEXT, SEXT, SITOFP, FPTOSI,
          BUILD_VECTOR, SPLAT, EXTRACT_ELEMENT, INSERT_ELEMENT, SHUFFLE_VECTOR, SELECT -> true;
      default -> false;
    };
  }

  static Expression expressionFor(Instruction instruction) {
    List<Object> operands = new ArrayList<>();
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      operands.add(valueKey(instruction.getOperand(index)));
    }
    String detail = switch (instruction.getOpcode()) {
      case ICMP, FCMP -> instruction.getPredicate();
      case CALL -> instruction.getCallee().getName();
      case GEP -> instruction.getGepSourceType() + ":" + instruction.isGepInbounds();
      default -> "";
    };
    return new Expression(
        instruction.getOpcode(), instruction.getType().toString(), detail, List.copyOf(operands));
  }

  private static Object valueKey(Value value) {
    if (value instanceof Constant.Int integer) {
      return new IntegerKey(integer.getType().dataType, integer.value);
    }
    if (value instanceof Constant.Float floating) {
      return new FloatKey(Float.floatToRawIntBits(floating.value));
    }
    if (value instanceof Constant.Zero zero) {
      return new ZeroKey(zero.getType().toString());
    }
    if (value instanceof Constant.Vector vector) {
      return new VectorKey(
          vector.getType().toString(), vector.elements.stream().map(GVNValueTable::valueKey).toList());
    }
    return value;
  }

  record Expression(
      Instruction.Opcode opcode, String type, String detail, List<Object> operands) {}

  private record IntegerKey(Type.DataType type, long value) {}

  private record FloatKey(int bits) {}

  private record ZeroKey(String type) {}

  private record VectorKey(String type, List<Object> elements) {}
}
