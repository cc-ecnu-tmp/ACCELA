package accela.backend.regalloc;

import accela.backend.frame.StackSlot;

public final class StackLocation implements ValueLocation {
  private final StackSlot slot;

  public StackLocation(StackSlot slot) {
    this.slot = slot;
  }

  public StackSlot getSlot() {
    return slot;
  }

  @Override
  public boolean isRegister() {
    return false;
  }

  @Override
  public boolean isStack() {
    return true;
  }
}
