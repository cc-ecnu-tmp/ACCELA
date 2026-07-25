package accela.backend.regalloc;

import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class AllocationResult {
  private final Map<VirtualRegister, ValueLocation> locations = new LinkedHashMap<>();
  private final List<PhysicalRegister> usedCalleeSavedRegisters = new ArrayList<>();

  public void put(VirtualRegister register, ValueLocation location) {
    locations.put(register, location);
  }

  public ValueLocation locationOf(VirtualRegister register) {
    return locations.get(register);
  }

  public Map<VirtualRegister, ValueLocation> getLocations() {
    return Collections.unmodifiableMap(locations);
  }

  public void addUsedCalleeSavedRegister(PhysicalRegister register) {
    for (PhysicalRegister existing : usedCalleeSavedRegisters) {
      if (existing.getName().equals(register.getName())) {
        return;
      }
    }
    usedCalleeSavedRegisters.add(register);
  }

  public List<PhysicalRegister> getUsedCalleeSavedRegisters() {
    return Collections.unmodifiableList(usedCalleeSavedRegisters);
  }
}
