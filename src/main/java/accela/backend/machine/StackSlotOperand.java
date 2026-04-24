package accela.backend.machine;

import accela.backend.frame.StackSlot;

public final class StackSlotOperand extends MachineOperand {
  private final StackSlot slot;

  public StackSlotOperand(StackSlot slot) {
    super(Kind.STACK_SLOT);
    this.slot = slot;
  }

  public StackSlot getSlot() {
    return slot;
  }
}
