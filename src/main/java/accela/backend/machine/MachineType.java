package accela.backend.machine;

import accela.ir.Type;

public enum MachineType {
  I1(4),
  I32(4),
  I64(8),
  PTR(8),
  F32(4),
  VECTOR(0),
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

  public boolean isVector() {
    return this == VECTOR;
  }

  public RegisterClass registerClass() {
    if (isVector()) return RegisterClass.VR;
    if (isFloat()) return RegisterClass.FPR;
    if (isIntegerLike()) return RegisterClass.GPR;
    throw new IllegalStateException("type has no register class: " + this);
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
        return VECTOR;
      default:
        throw new IllegalArgumentException("unsupported IR type: " + type);
    }
  }
}
