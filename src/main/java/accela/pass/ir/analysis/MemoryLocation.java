package accela.pass.ir.analysis;

import accela.ir.ConstantFolding;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.Objects;

/** A typed byte range accessed through an opaque IR pointer. */
public record MemoryLocation(Value pointer, Type accessType, long byteSize) {
  public MemoryLocation {
    Objects.requireNonNull(pointer, "memory location pointer");
    Objects.requireNonNull(accessType, "memory location access type");
    if (byteSize <= 0) throw new IllegalArgumentException("memory location must access bytes");
  }

  /** Returns the location read or written by a load/store, or {@code null} for other opcodes. */
  public static MemoryLocation fromInstruction(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case LOAD -> of(instruction.getOperand(0), instruction.getType());
      case STORE -> of(instruction.getOperand(1), instruction.getOperand(0).getType());
      default -> null;
    };
  }

  public static MemoryLocation of(Value pointer, Type accessType) {
    return new MemoryLocation(pointer, accessType, byteSize(accessType));
  }

  /** Returns the storage width used by the current target-independent IR layout. */
  public static long byteSize(Type type) {
    if (type.isArray() || type.isVector()) {
      return Math.multiplyExact(type.size, byteSize(type.innerType));
    }
    if (type == Type.I64 || type.isPointer()) return Long.BYTES;
    if (type == Type.I1) return 1;
    if (type == Type.INT || type == Type.FLOAT) return Integer.BYTES;
    throw new IllegalArgumentException("type has no memory representation: " + type);
  }

  /** Exact address and access-shape equality; suitable for load forwarding. */
  public boolean isSameAccess(MemoryLocation other) {
    return pointer == other.pointer && hasSameAccessShape(other);
  }

  public boolean hasSameAccessShape(MemoryLocation other) {
    return accessType.equals(other.accessType) && byteSize == other.byteSize;
  }

  /** Whether this write, starting at the same address, overwrites every byte of {@code other}. */
  public boolean fullyCovers(MemoryLocation other) {
    return pointer == other.pointer && byteSize >= other.byteSize;
  }

  /** Whether two half-open byte ranges are disjoint for a known {@code left - right} offset. */
  public static boolean areDisjointAtOffset(
      long leftMinusRight, long leftSize, long rightSize) {
    return leftMinusRight >= rightSize || leftMinusRight <= -leftSize;
  }

  /** Whether constant byte ranges prove that two accesses do not overlap. */
  public boolean isKnownDisjoint(MemoryLocation other) {
    ConstantRange left = constantRange();
    ConstantRange right = other.constantRange();
    if (left == null || right == null || left.base != right.base) return false;
    try {
      return areDisjointAtOffset(
          Math.subtractExact(left.byteOffset, right.byteOffset), byteSize, other.byteSize);
    } catch (ArithmeticException overflow) {
      return false;
    }
  }

  /** Whether this write's constant byte range fully covers {@code other}. */
  public boolean isKnownToCover(MemoryLocation other) {
    if (fullyCovers(other)) return true;
    ConstantRange later = constantRange();
    ConstantRange earlier = other.constantRange();
    if (later == null || earlier == null || later.base != earlier.base) return false;
    try {
      long laterEnd = Math.addExact(later.byteOffset, byteSize);
      long earlierEnd = Math.addExact(earlier.byteOffset, other.byteSize);
      return later.byteOffset <= earlier.byteOffset && laterEnd >= earlierEnd;
    } catch (ArithmeticException overflow) {
      return false;
    }
  }

  private ConstantRange constantRange() {
    if (!(pointer instanceof Instruction gep)
        || !(PointerProvenance.root(pointer) instanceof GlobalVariable global)) return null;
    Integer leaf = ConstantFolding.constantArrayIndex(global, gep);
    if (leaf == null) return null;
    try {
      return new ConstantRange(
          global,
          Math.multiplyExact((long) leaf, byteSize(arrayLeafType(global.getValueType()))));
    } catch (ArithmeticException overflow) {
      return null;
    }
  }

  private static Type arrayLeafType(Type type) {
    while (type.isArray()) type = type.innerType;
    return type;
  }

  private record ConstantRange(Value base, long byteOffset) {}
}
