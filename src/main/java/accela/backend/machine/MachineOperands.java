package accela.backend;

abstract class MachineOperand {
  enum Kind {
    VREG,
    PHYS_REG,
    IMM,
    FLOAT_IMM,
    BLOCK,
    SYMBOL,
    STACK_SLOT
  }

  private final Kind kind;

  MachineOperand(Kind kind) {
    this.kind = kind;
  }

  Kind getKind() {
    return kind;
  }
}

final class VRegOperand extends MachineOperand {
  private final VirtualRegister register;

  VRegOperand(VirtualRegister register) {
    super(Kind.VREG);
    this.register = register;
  }

  VirtualRegister getRegister() {
    return register;
  }
}

final class PhysicalRegOperand extends MachineOperand {
  private final PhysicalRegister register;

  PhysicalRegOperand(PhysicalRegister register) {
    super(Kind.PHYS_REG);
    this.register = register;
  }

  PhysicalRegister getRegister() {
    return register;
  }
}

final class ImmOperand extends MachineOperand {
  private final long value;

  ImmOperand(long value) {
    super(Kind.IMM);
    this.value = value;
  }

  long getValue() {
    return value;
  }
}

final class FloatImmOperand extends MachineOperand {
  private final float value;

  FloatImmOperand(float value) {
    super(Kind.FLOAT_IMM);
    this.value = value;
  }

  float getValue() {
    return value;
  }
}

final class BlockOperand extends MachineOperand {
  private final MachineBasicBlock block;

  BlockOperand(MachineBasicBlock block) {
    super(Kind.BLOCK);
    this.block = block;
  }

  MachineBasicBlock getBlock() {
    return block;
  }
}

final class SymbolOperand extends MachineOperand {
  private final String symbol;

  SymbolOperand(String symbol) {
    super(Kind.SYMBOL);
    this.symbol = symbol;
  }

  String getSymbol() {
    return symbol;
  }
}

final class StackSlotOperand extends MachineOperand {
  private final StackSlot slot;

  StackSlotOperand(StackSlot slot) {
    super(Kind.STACK_SLOT);
    this.slot = slot;
  }

  StackSlot getSlot() {
    return slot;
  }
}
