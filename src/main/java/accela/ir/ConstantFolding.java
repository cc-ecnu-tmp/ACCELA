package accela.ir;

/** Constant-folding utilities shared by IR transforms. */
public final class ConstantFolding {
  private ConstantFolding() {}

  public static Integer constantArrayIndex(GlobalVariable global, Instruction gep) {
    Type arrayType = global.getValueType();
    if (gep.getOpcode() != Instruction.Opcode.GEP
        || gep.getOperand(0) != global
        || !arrayType.isArray()) return null;
    if (gep.getGepSourceType() == arrayType) {
      Long leadingIndex =
          gep.getNumOperands() < 2 ? null : constantIndex(gep.getOperand(1));
      if (gep.getNumOperands() < 3 || leadingIndex == null || leadingIndex != 0) return null;
      int leaf = 0;
      Type current = arrayType;
      for (int index = 2; index < gep.getNumOperands(); index++) {
        if (!current.isArray()) return null;
        Long subscript = constantIndex(gep.getOperand(index));
        if (subscript == null || subscript < 0 || subscript >= current.size) return null;
        leaf += subscript.intValue() * leafCount(current.innerType);
        current = current.innerType;
      }
      return current.isArray() ? null : leaf;
    }
    if (gep.getGepSourceType() == arrayType.scalarType() && gep.getNumOperands() == 2) {
      Long index = constantIndex(gep.getOperand(1));
      int leaves = leafCount(arrayType);
      return index != null && index >= 0 && index < leaves ? index.intValue() : null;
    }
    return null;
  }

  public static Constant initializerAt(GlobalVariable global, int leafIndex) {
    return initializerAt(global.getInitializer(), global.getValueType(), leafIndex);
  }

  private static Constant initializerAt(Constant constant, Type type, int leafIndex) {
    if (!type.isArray()) return constant;
    int stride = leafCount(type.innerType);
    int elementIndex = leafIndex / stride;
    Constant element = Constant.zero(type.innerType);
    if (constant instanceof Constant.Array array && elementIndex < array.elements.size()) {
      element = array.elements.get(elementIndex);
    }
    return initializerAt(element, type.innerType, leafIndex % stride);
  }

  private static Long constantIndex(Value value) {
    return value instanceof Constant.Int integer ? integer.value : null;
  }

  private static int leafCount(Type type) {
    return type.isArray() ? type.size * leafCount(type.innerType) : 1;
  }
}
