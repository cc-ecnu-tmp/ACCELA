package accela.backend.target;

import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegister;
import accela.ir.Constant;
import accela.ir.Type;
import java.util.List;

public final class RISCVTarget {
  private static final int MEMZERO_HELPER_THRESHOLD = 128;
  static final PhysicalRegister ZERO = new PhysicalRegister("zero", MachineType.I32);
  static final PhysicalRegister SP = new PhysicalRegister("sp", MachineType.PTR);
  static final PhysicalRegister S0 = new PhysicalRegister("s0", MachineType.PTR);
  static final PhysicalRegister RA = new PhysicalRegister("ra", MachineType.PTR);

  private final List<PhysicalRegister> intScratch =
      List.of(
          new PhysicalRegister("a4", MachineType.I32),
          new PhysicalRegister("a5", MachineType.I32),
          new PhysicalRegister("a6", MachineType.I32),
          new PhysicalRegister("a7", MachineType.I32));
  private final List<PhysicalRegister> floatScratch =
      List.of(
          new PhysicalRegister("fa4", MachineType.F32),
          new PhysicalRegister("fa5", MachineType.F32),
          new PhysicalRegister("fa6", MachineType.F32),
          new PhysicalRegister("fa7", MachineType.F32));

  public List<PhysicalRegister> getIntScratch() {
    return intScratch;
  }

  public List<PhysicalRegister> getFloatScratch() {
    return floatScratch;
  }

  public PhysicalRegister getReturnRegister(MachineType type) {
    if (type.isFloat()) return new PhysicalRegister("fa0", MachineType.F32);
    return new PhysicalRegister("a0", type == MachineType.PTR ? MachineType.PTR : MachineType.I32);
  }

  public PhysicalRegister getArgRegister(int index, MachineType type) {
    if (type.isFloat()) return new PhysicalRegister("fa" + index, MachineType.F32);
    return new PhysicalRegister("a" + index, type == MachineType.PTR ? MachineType.PTR : MachineType.I32);
  }

  public int stackSizeOf(MachineType type) {
    if (type == MachineType.PTR) return 8;
    if (type == MachineType.I64) return 8;
    if (type == MachineType.VOID) return 0;
    return 4;
  }

  public int stackAlignOf(MachineType type) {
    if (type == MachineType.PTR) return 8;
    if (type == MachineType.I64) return 8;
    if (type == MachineType.VOID) return 1;
    return 4;
  }

  public int sizeOfIrType(Type type) {
    if (type == null) return 0;
    switch (type.dataType) {
      case I1:
      case INT:
      case FLOAT:
        return 4;
      case POINTER:
        return 8;
      case I64:
        return 8;
      case ARRAY:
        return type.size * sizeOfIrType(type.innerType);
      case VOID:
      default:
        return 0;
    }
  }

  public int alignOfIrType(Type type) {
    if (type == null) return 1;
    if (type.dataType == Type.DataType.ARRAY) {
      return Math.max(stackAlignOf(MachineType.PTR), alignOfIrType(type.innerType));
    }
    return stackAlignOf(MachineType.fromIr(type));
  }

  public int alignTo(int value, int align) {
    if (align <= 1) return value;
    return ((value + align - 1) / align) * align;
  }

  public boolean shouldUseMemzeroHelper(int bytes) {
    return bytes > MEMZERO_HELPER_THRESHOLD;
  }

  public long lowerIntConstant(Constant.Int constant) {
    return constant.value;
  }

  public int lowerFloatBits(Constant.Float constant) {
    return java.lang.Float.floatToRawIntBits(constant.value);
  }

  public int callStackSlotSize(MachineType type) {
    if (type == MachineType.VOID) return 0;
    return 8;
  }

  public CallArgCursor newCallArgCursor() {
    return new CallArgCursor();
  }

  public CallArgAssignment assignCallArg(CallArgCursor cursor, MachineType type) {
    if (type.isFloat()) {
      if (cursor.nextFloatArg < 8) {
        return CallArgAssignment.inRegister(getArgRegister(cursor.nextFloatArg++, type));
      }
    }

    // A floating-point value falls back to the integer calling convention once
    // fa0-fa7 are exhausted.
    if (cursor.nextIntArg < 8) {
      MachineType registerType = type.isFloat() ? MachineType.I32 : type;
      return CallArgAssignment.inRegister(getArgRegister(cursor.nextIntArg++, registerType));
    }

    int stackOffset = cursor.nextStackOffset;
    cursor.nextStackOffset += callStackSlotSize(type);
    return CallArgAssignment.onStack(stackOffset);
  }

  public static final class CallArgCursor {
    private int nextIntArg = 0;
    private int nextFloatArg = 0;
    private int nextStackOffset = 0;

    public int getStackBytes() {
      return nextStackOffset;
    }
  }

  public static final class CallArgAssignment {
    private final PhysicalRegister register;
    private final int stackOffset;

    private CallArgAssignment(PhysicalRegister register, int stackOffset) {
      this.register = register;
      this.stackOffset = stackOffset;
    }

    public static CallArgAssignment inRegister(PhysicalRegister register) {
      return new CallArgAssignment(register, -1);
    }

    public static CallArgAssignment onStack(int stackOffset) {
      return new CallArgAssignment(null, stackOffset);
    }

    public boolean isInRegister() {
      return register != null;
    }

    public PhysicalRegister getRegister() {
      return register;
    }

    public int getStackOffset() {
      return stackOffset;
    }
  }
}
