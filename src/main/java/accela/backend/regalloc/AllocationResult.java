package accela.backend.regalloc;

import accela.backend.machine.VirtualRegister;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

public final class AllocationResult {
  private final Map<VirtualRegister, ValueLocation> locations = new LinkedHashMap<>();

  public void put(VirtualRegister register, ValueLocation location) {
    locations.put(register, location);
  }

  public ValueLocation locationOf(VirtualRegister register) {
    return locations.get(register);
  }

  public Map<VirtualRegister, ValueLocation> getLocations() {
    return Collections.unmodifiableMap(locations);
  }
}
