package accela.pass.ir.analysis.alias;

import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.List;

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

  /** Returns shape/stride and an exact constant region when a GEP chain is fully constant. */
  public static ArrayProvenance analyze(Value pointer) {
    Value object = root(pointer);
    Type objectType = objectType(object);
    Type elementType = objectType == null ? Type.INT : objectType.scalarType();
    List<Integer> shape = new ArrayList<>();
    List<Long> strides = new ArrayList<>();
    if (objectType != null) collectShape(objectType, shape, strides);
    List<Long> indices = new ArrayList<>();
    long offset = 0;
    boolean exact = objectType != null;
    Value current = pointer;
    List<Instruction> chain = new ArrayList<>();
    while (current instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.GEP) {
      chain.add(instruction);
      current = instruction.getOperand(0);
    }
    for (int chainIndex = chain.size() - 1; chainIndex >= 0; chainIndex--) {
      Instruction gep = chain.get(chainIndex);
      Type source = gep.getGepSourceType();
      for (int operand = 1; operand < gep.getNumOperands(); operand++) {
        if (!(gep.getOperand(operand) instanceof accela.ir.Constant.Int index)
            || source == null) {
          exact = false;
          indices.add(null);
          if (source != null && source.isArray()) source = source.innerType;
          continue;
        }
        long stride = byteSize(source);
        try {
          offset = Math.addExact(offset, Math.multiplyExact(index.value, stride));
        } catch (ArithmeticException overflow) {
          exact = false;
        }
        if (offset < 0) exact = false;
        indices.add(index.value);
        if (source.isArray()) source = source.innerType;
      }
    }
    // The pointer value does not carry the type of the eventual load/store in this IR.  Keep a
    // four-byte minimum so a typed i32 access through a byte-addressed SysY array cannot be
    // incorrectly treated as a disjoint one-byte access.
    long width = Math.max(4, byteSize(elementType));
    MemoryRegion region = new MemoryRegion(object, Math.max(0, offset), width, exact, indices);
    return new ArrayProvenance(object, elementType, shape, strides, region);
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

  /** Region-aware alias query reserved for candidates that have opted into exact SysY facts. */
  public static boolean mayAliasWithRegions(Value left, Value right) {
    if (left == right) return true;
    ArrayProvenance leftFacts = analyze(left);
    ArrayProvenance rightFacts = analyze(right);
    if (leftFacts.region().disjoint(rightFacts.region())) return false;
    return mayAlias(left, right);
  }

  private static boolean isAlloca(Value value) {
    return value instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.ALLOCA;
  }

  private static Type objectType(Value object) {
    if (object instanceof GlobalVariable global) return global.getValueType();
    if (object instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.ALLOCA) {
      return instruction.getAllocatedType();
    }
    return null;
  }

  private static void collectShape(Type type, List<Integer> shape, List<Long> strides) {
    long stride = byteSize(type);
    while (type != null && type.isArray()) {
      shape.add(type.size);
      strides.add(stride / Math.max(1, type.size));
      type = type.innerType;
      stride = byteSize(type);
    }
  }

  private static long byteSize(Type type) {
    if (type == null) return 0;
    if (type.isArray()) return Math.multiplyExact(type.size, byteSize(type.innerType));
    if (type == Type.I64 || type.isPointer()) return 8;
    if (type == Type.I1) return 1;
    return 4;
  }
}
