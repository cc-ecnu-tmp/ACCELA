package accela.backend.machine;

public final class PhysicalRegOperand extends MachineOperand {
  private final PhysicalRegister register;

  public PhysicalRegOperand(PhysicalRegister register) {
    super(Kind.PHYS_REG);
    this.register = register;
  }

  public PhysicalRegister getRegister() {
    return register;
  }
}
