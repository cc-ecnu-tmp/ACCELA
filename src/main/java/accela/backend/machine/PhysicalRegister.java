package accela.backend.machine;

import java.util.Objects;
import java.util.Set;

public final class PhysicalRegister {
  private final String name;
  private final MachineType type;
  private final RegisterClass registerClass;
  private final int encoding;
  private final Set<Integer> aliasUnits;

  public PhysicalRegister(String name, MachineType type) {
    this(name, type, type.registerClass(), -1, Set.of());
  }

  public PhysicalRegister(
      String name,
      MachineType type,
      RegisterClass registerClass,
      int encoding,
      Set<Integer> aliasUnits) {
    this.name = name;
    this.type = type;
    this.registerClass = registerClass;
    this.encoding = encoding;
    this.aliasUnits = Set.copyOf(aliasUnits);
  }

  public String getName() {
    return name;
  }

  public MachineType getType() {
    return type;
  }

  public RegisterClass getRegisterClass() {
    return registerClass;
  }

  public int getEncoding() {
    return encoding;
  }

  public Set<Integer> getAliasUnits() {
    return aliasUnits;
  }

  public boolean overlaps(PhysicalRegister other) {
    if (registerClass != other.registerClass) return false;
    if (aliasUnits.isEmpty() || other.aliasUnits.isEmpty()) return name.equals(other.name);
    return aliasUnits.stream().anyMatch(other.aliasUnits::contains);
  }

  @Override
  public boolean equals(Object other) {
    if (!(other instanceof PhysicalRegister register)) return false;
    return name.equals(register.name)
        && registerClass == register.registerClass
        && aliasUnits.equals(register.aliasUnits);
  }

  @Override
  public int hashCode() {
    return Objects.hash(name, registerClass, aliasUnits);
  }
}
