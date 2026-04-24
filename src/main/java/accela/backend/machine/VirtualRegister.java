package accela.backend.machine;

public final class VirtualRegister {
  private final int id;
  private final MachineType type;
  private final String hint;

  public VirtualRegister(int id, MachineType type, String hint) {
    this.id = id;
    this.type = type;
    this.hint = hint;
  }

  public int getId() {
    return id;
  }

  public MachineType getType() {
    return type;
  }

  public String getHint() {
    return hint;
  }

  @Override
  public boolean equals(Object other) {
    if (!(other instanceof VirtualRegister)) return false;
    return id == ((VirtualRegister) other).id;
  }

  @Override
  public int hashCode() {
    return id;
  }

  @Override
  public String toString() {
    return "%v" + id;
  }
}
