package accela.ir;

/**
 * A use-def chain link. Represents one operand slot in an Instruction
 * that references a Value.
 *
 * When the referenced Value is replaced, this Use automatically
 * updates to point to the new Value.
 */
public class Use {
  private Value value;
  private final Instruction user;
  private final int operandIndex;

  Use(Value value, Instruction user, int operandIndex) {
    this.value = value;
    this.user = user;
    this.operandIndex = operandIndex;
    if (value != null) value.addUse(this);
  }

  public Value getValue() {
    return value;
  }

  public Instruction getUser() {
    return user;
  }

  public int getOperandIndex() {
    return operandIndex;
  }

  /** Replace the referenced value, maintaining use-lists on both old and new. */
  void setValue(Value newValue) {
    if (value != null) value.removeUse(this);
    value = newValue;
    if (newValue != null) newValue.addUse(this);
  }

  void drop() {
    if (value != null) value.removeUse(this);
    value = null;
  }
}
