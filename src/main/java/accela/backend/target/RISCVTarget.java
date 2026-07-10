package accela.backend.target;

import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegister;
import accela.ir.Constant;
import accela.ir.Type;
import java.util.List;

public final class RISCVTarget {
  private static final int MEMSET_LIBCALL_THRESHOLD = 32;
  static final PhysicalRegister ZERO = new PhysicalRegister("zero", MachineType.I32);
  static final PhysicalRegister SP = new PhysicalRegister("sp", MachineType.PTR);
  static final PhysicalRegister S0 = new PhysicalRegister("s0", MachineType.PTR);
  static final PhysicalRegister RA = new PhysicalRegister("ra", MachineType.PTR);

  private final List<PhysicalRegister> intScratch =
      List.of(
          new PhysicalRegister("t0", MachineType.I32),
          new PhysicalRegister("t1", MachineType.I32),
          new PhysicalRegister("t2", MachineType.I32),
          new PhysicalRegister("t3", MachineType.I32));
  private final List<PhysicalRegister> floatScratch =
      List.of(
          new PhysicalRegister("ft0", MachineType.F32),
          new PhysicalRegister("ft1", MachineType.F32),
          new PhysicalRegister("ft2", MachineType.F32),
          new PhysicalRegister("ft3", MachineType.F32));
  private final List<PhysicalRegister> intAllocatable =
      List.of(
          new PhysicalRegister("t4", MachineType.I32),
          new PhysicalRegister("t5", MachineType.I32),
          new PhysicalRegister("t6", MachineType.I32));
  private final List<PhysicalRegister> floatAllocatable =
      List.of(
          new PhysicalRegister("ft4", MachineType.F32),
          new PhysicalRegister("ft5", MachineType.F32),
          new PhysicalRegister("ft6", MachineType.F32),
          new PhysicalRegister("ft7", MachineType.F32),
          new PhysicalRegister("ft8", MachineType.F32),
          new PhysicalRegister("ft9", MachineType.F32),
          new PhysicalRegister("ft10", MachineType.F32),
          new PhysicalRegister("ft11", MachineType.F32));

  public List<PhysicalRegister> getIntScratch() {
    return intScratch;
  }

  public List<PhysicalRegister> getFloatScratch() {
    return floatScratch;
  }

  public List<PhysicalRegister> getAllocatableRegisters(MachineType type) {
    return type.isFloat() ? floatAllocatable : intAllocatable;
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
    if (type.dataType == Type.DataType.ARRAY) return alignOfIrType(type.innerType);
    return stackAlignOf(MachineType.fromIr(type));
  }

  public int alignTo(int value, int align) {
    if (align <= 1) return value;
    return ((value + align - 1) / align) * align;
  }

  public boolean shouldUseMemsetLibcall(int bytes) {
    return bytes > MEMSET_LIBCALL_THRESHOLD;
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
    } else {
      if (cursor.nextIntArg < 8) {
        return CallArgAssignment.inRegister(getArgRegister(cursor.nextIntArg++, type));
      }
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
