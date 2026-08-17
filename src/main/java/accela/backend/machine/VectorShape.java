package accela.backend.machine;

import accela.ir.Type;
import java.util.Objects;

/** Fixed-width IR vector shape plus its allocation and vector-state grouping. */
public record VectorShape(
    MachineType elementType, int lanes, boolean mask, int lmul, int stateLmul) {
  public VectorShape(MachineType elementType, int lanes, boolean mask, int lmul) {
    this(elementType, lanes, mask, lmul, lmul);
  }

  public VectorShape {
    Objects.requireNonNull(elementType, "vector element type");
    if (elementType != MachineType.I1
        && elementType != MachineType.I32
        && elementType != MachineType.I64
        && elementType != MachineType.F32) {
      throw new IllegalArgumentException("unsupported vector element type: " + elementType);
    }
    if (lanes <= 0) throw new IllegalArgumentException("vector lanes must be positive");
    if (lmul != 1 && lmul != 2 && lmul != 4 && lmul != 8) {
      throw new IllegalArgumentException("LMUL must be 1, 2, 4, or 8");
    }
    if (stateLmul != 1 && stateLmul != 2 && stateLmul != 4 && stateLmul != 8) {
      throw new IllegalArgumentException("state LMUL must be 1, 2, 4, or 8");
    }
  }

  public int sew() {
    return switch (elementType) {
      case I1 -> 8;
      case I32, F32 -> 32;
      case I64 -> 64;
      default -> throw new IllegalStateException("not a vector element type: " + elementType);
    };
  }

  public int semanticSizeBytes() {
    if (mask) return Math.max(1, (lanes + 7) / 8);
    return (lanes * sew() + 7) / 8;
  }

  public static VectorShape fromIr(Type type, int minimumVLEN) {
    if (!type.isVector()) throw new IllegalArgumentException("expected vector type: " + type);
    MachineType element = MachineType.fromIr(type.getElementType());
    boolean mask = type.getElementType() == Type.I1;
    long bits = (long) type.getLaneCount() * (mask ? 1L : element.getSize() * 8L);
    int lmul = 1;
    while ((long) minimumVLEN * lmul < bits && lmul < 8) lmul *= 2;
    if ((long) minimumVLEN * lmul < bits) {
      throw new IllegalArgumentException(
          "fixed vector " + type + " exceeds LMUL=8 at VLEN=" + minimumVLEN);
    }
    int stateLmul = lmul;
    if (mask) {
      stateLmul = 1;
      long stateBits = (long) type.getLaneCount() * 8L;
      while ((long) minimumVLEN * stateLmul < stateBits && stateLmul < 8) stateLmul *= 2;
      if ((long) minimumVLEN * stateLmul < stateBits) {
        throw new IllegalArgumentException(
            "mask vector " + type + " exceeds architectural VLMAX at VLEN=" + minimumVLEN);
      }
      lmul = 1;
    }
    return new VectorShape(element, type.getLaneCount(), mask, lmul, stateLmul);
  }
}
