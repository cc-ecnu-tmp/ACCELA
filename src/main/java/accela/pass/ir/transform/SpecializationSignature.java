package accela.pass.ir.transform;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.List;

record SpecializationSignature(List<SpecializationSignature.Binding> bindings) {
  SpecializationSignature {
    bindings = List.copyOf(bindings);
  }

  static SpecializationSignature fromCall(Instruction call) {
    List<Binding> bindings = new ArrayList<>();
    for (int i = 0; i < call.getNumOperands(); i++) {
      Binding binding = Binding.from(i, call.getOperand(i));
      if (binding != null) bindings.add(binding);
    }
    return new SpecializationSignature(bindings);
  }

  boolean isEmpty() {
    return bindings.isEmpty();
  }

  void applyTo(Function function) {
    for (Binding binding : bindings) {
      function.getArguments().get(binding.argument()).replaceAllUsesWith(binding.constant());
    }
  }

  record Binding(int argument, Type type, long bits) {
    static Binding from(int argument, Value value) {
      if (value instanceof Constant.Int integer) {
        return new Binding(argument, value.getType(), integer.value);
      }
      if (value instanceof Constant.Float floating) {
        return new Binding(
            argument, Type.FLOAT, java.lang.Float.floatToRawIntBits(floating.value));
      }
      if (value instanceof Constant.Zero && isScalar(value.getType())) {
        return new Binding(argument, value.getType(), 0);
      }
      return null;
    }

    private static boolean isScalar(Type type) {
      return type == Type.I1 || type == Type.INT || type == Type.I64 || type == Type.FLOAT;
    }

    Constant constant() {
      if (type == Type.FLOAT) {
        return Constant.floatConst(java.lang.Float.intBitsToFloat((int) bits));
      }
      if (type == Type.I1) return Constant.boolConst(bits != 0);
      if (type == Type.I64) return Constant.int64Const(bits);
      return Constant.intConst(bits);
    }
  }
}
