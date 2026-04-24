package accela.backend.machine;

public final class PhysicalRegister {
  private final String name;
  private final MachineType type;

  public PhysicalRegister(String name, MachineType type) {
    this.name = name;
    this.type = type;
  }

  public String getName() {
    return name;
  }

  public MachineType getType() {
    return type;
  }
}
