package accela.backend.machine;

public abstract class MachineOperand {
  public enum Kind {
    VREG,
    PHYS_REG,
    IMM,
    FLOAT_IMM,
    BLOCK,
    SYMBOL,
    STACK_SLOT,
    VECTOR_CONSTANT
  }

  private final Kind kind;

  protected MachineOperand(Kind kind) {
    this.kind = kind;
  }

  public Kind getKind() {
    return kind;
  }
}
