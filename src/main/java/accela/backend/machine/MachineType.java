package accela.backend.machine;

import accela.ir.Type;

public enum MachineType {
  I1(4),
  I32(4),
  I64(8),
  PTR(8),
  F32(4),
  VOID(0);

  private final int size;

  MachineType(int size) {
    this.size = size;
  }

  public int getSize() {
    return size;
  }

  public boolean isFloat() {
    return this == F32;
  }

  public boolean isIntegerLike() {
    return this == I1 || this == I32 || this == I64 || this == PTR;
  }

  public static MachineType fromIr(Type type) {
    if (type == null) return VOID;
    switch (type.dataType) {
      case I1:
        return I1;
      case INT:
        return I32;
      case I64:
        return I64;
      case FLOAT:
        return F32;
      case POINTER:
        return PTR;
      case VOID:
        return VOID;
      case ARRAY:
        return PTR;
      case VECTOR:
        throw new IllegalArgumentException(
            "vector IR must be scalarized before machine type selection: " + type);
      default:
        throw new IllegalArgumentException("unsupported IR type: " + type);
    }
  }
}
