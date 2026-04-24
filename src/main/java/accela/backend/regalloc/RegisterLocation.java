package accela.backend.regalloc;

import accela.backend.machine.PhysicalRegister;

public final class RegisterLocation implements ValueLocation {
  private final PhysicalRegister register;

  public RegisterLocation(PhysicalRegister register) {
    this.register = register;
  }

  public PhysicalRegister getRegister() {
    return register;
  }

  @Override
  public boolean isRegister() {
    return true;
  }

  @Override
  public boolean isStack() {
    return false;
  }
}
