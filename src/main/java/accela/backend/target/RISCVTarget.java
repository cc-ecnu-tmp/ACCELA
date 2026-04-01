package accela.backend;

import accela.ir.Constant;
import accela.ir.Type;
import java.util.List;

final class RISCVTarget {
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

  List<PhysicalRegister> getIntScratch() {
    return intScratch;
  }

  List<PhysicalRegister> getFloatScratch() {
    return floatScratch;
  }

  PhysicalRegister getReturnRegister(MachineType type) {
    if (type.isFloat()) return new PhysicalRegister("fa0", MachineType.F32);
    return new PhysicalRegister("a0", type == MachineType.PTR ? MachineType.PTR : MachineType.I32);
  }

  PhysicalRegister getArgRegister(int index, MachineType type) {
    if (type.isFloat()) return new PhysicalRegister("fa" + index, MachineType.F32);
    return new PhysicalRegister("a" + index, type == MachineType.PTR ? MachineType.PTR : MachineType.I32);
  }

  int stackSizeOf(MachineType type) {
    if (type == MachineType.PTR) return 8;
    if (type == MachineType.I64) return 8;
    if (type == MachineType.VOID) return 0;
    return 4;
  }

  int stackAlignOf(MachineType type) {
    if (type == MachineType.PTR) return 8;
    if (type == MachineType.I64) return 8;
    if (type == MachineType.VOID) return 1;
    return 4;
  }

  int sizeOfIrType(Type type) {
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

  int alignOfIrType(Type type) {
    if (type == null) return 1;
    if (type.dataType == Type.DataType.ARRAY) return alignOfIrType(type.innerType);
    return stackAlignOf(MachineType.fromIr(type));
  }

  int alignTo(int value, int align) {
    if (align <= 1) return value;
    return ((value + align - 1) / align) * align;
  }

  long lowerIntConstant(Constant.Int constant) {
    return constant.value;
  }

  int lowerFloatBits(Constant.Float constant) {
    return java.lang.Float.floatToRawIntBits(constant.value);
  }

  int callStackSlotSize(MachineType type) {
    if (type == MachineType.VOID) return 0;
    return 8;
  }

  CallArgCursor newCallArgCursor() {
    return new CallArgCursor();
  }

  CallArgAssignment assignCallArg(CallArgCursor cursor, MachineType type) {
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

  static final class CallArgCursor {
    private int nextIntArg = 0;
    private int nextFloatArg = 0;
    private int nextStackOffset = 0;

    int getStackBytes() {
      return nextStackOffset;
    }
  }

  static final class CallArgAssignment {
    private final PhysicalRegister register;
    private final int stackOffset;

    private CallArgAssignment(PhysicalRegister register, int stackOffset) {
      this.register = register;
      this.stackOffset = stackOffset;
    }

    static CallArgAssignment inRegister(PhysicalRegister register) {
      return new CallArgAssignment(register, -1);
    }

    static CallArgAssignment onStack(int stackOffset) {
      return new CallArgAssignment(null, stackOffset);
    }

    boolean isInRegister() {
      return register != null;
    }

    PhysicalRegister getRegister() {
      return register;
    }

    int getStackOffset() {
      return stackOffset;
    }
  }
}
