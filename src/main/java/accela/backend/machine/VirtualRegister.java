package accela.backend.machine;

public final class VirtualRegister {
  private final int id;
  private final MachineType type;
  private final String hint;
  private final VectorShape vectorShape;

  public VirtualRegister(int id, MachineType type, String hint) {
    this(id, type, hint, null);
  }

  public VirtualRegister(int id, MachineType type, String hint, VectorShape vectorShape) {
    this.id = id;
    this.type = type;
    this.hint = hint;
    if (type.isVector() != (vectorShape != null)) {
      throw new IllegalArgumentException("vector machine type and shape must be specified together");
    }
    this.vectorShape = vectorShape;
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

  public RegisterClass getRegisterClass() {
    return type.registerClass();
  }

  public VectorShape getVectorShape() {
    return vectorShape;
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
