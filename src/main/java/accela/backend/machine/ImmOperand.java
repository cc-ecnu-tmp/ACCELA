package accela.backend.machine;

public final class ImmOperand extends MachineOperand {
  private final long value;

  public ImmOperand(long value) {
    super(Kind.IMM);
    this.value = value;
  }

  public long getValue() {
    return value;
  }
}
