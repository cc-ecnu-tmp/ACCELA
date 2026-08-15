package accela.pass.ir.analysis;

import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
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
}
