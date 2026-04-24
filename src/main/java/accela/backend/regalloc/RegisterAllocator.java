package accela.backend.regalloc;

import accela.backend.machine.MachineFunction;
import accela.backend.target.RISCVTarget;

public interface RegisterAllocator {
  AllocationResult allocate(MachineFunction function, RISCVTarget target);
}
