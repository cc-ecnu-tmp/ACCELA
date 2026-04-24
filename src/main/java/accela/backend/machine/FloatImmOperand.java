package accela.backend.machine;

public final class FloatImmOperand extends MachineOperand {
  private final float value;

  public FloatImmOperand(float value) {
    super(Kind.FLOAT_IMM);
    this.value = value;
  }

  public float getValue() {
    return value;
  }
}
