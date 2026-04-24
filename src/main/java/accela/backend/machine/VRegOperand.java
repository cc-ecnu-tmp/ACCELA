package accela.backend.machine;

public final class VRegOperand extends MachineOperand {
  private final VirtualRegister register;

  public VRegOperand(VirtualRegister register) {
    super(Kind.VREG);
    this.register = register;
  }

  public VirtualRegister getRegister() {
    return register;
  }
}
