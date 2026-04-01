package accela.backend;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

interface ValueLocation {
  boolean isRegister();

  boolean isStack();
}

final class RegisterLocation implements ValueLocation {
  private final PhysicalRegister register;

  RegisterLocation(PhysicalRegister register) {
    this.register = register;
  }

  PhysicalRegister getRegister() {
    return register;
  }

  @Override
  public boolean isRegister() {
    return true;
  }

  @Override
  public boolean isStack() {
    return false;
  }
}

final class StackLocation implements ValueLocation {
  private final StackSlot slot;

  StackLocation(StackSlot slot) {
    this.slot = slot;
  }

  StackSlot getSlot() {
    return slot;
  }

  @Override
  public boolean isRegister() {
    return false;
  }

  @Override
  public boolean isStack() {
    return true;
  }
}

final class AllocationResult {
  private final Map<VirtualRegister, ValueLocation> locations = new LinkedHashMap<>();

  void put(VirtualRegister register, ValueLocation location) {
    locations.put(register, location);
  }

  ValueLocation locationOf(VirtualRegister register) {
    return locations.get(register);
  }

  Map<VirtualRegister, ValueLocation> getLocations() {
    return Collections.unmodifiableMap(locations);
  }
}

interface RegisterAllocator {
  AllocationResult allocate(MachineFunction function, RISCVTarget target);
}

final class AllSpillRegisterAllocator implements RegisterAllocator {
  @Override
  public AllocationResult allocate(MachineFunction function, RISCVTarget target) {
    AllocationResult result = new AllocationResult();
    Map<VirtualRegister, StackSlot> spills = new LinkedHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getDest() != null) ensureSlot(function, spills, instr.getDest(), target);
        for (MachineOperand operand : instr.getOperands()) {
          if (operand instanceof VRegOperand) {
            ensureSlot(function, spills, ((VRegOperand) operand).getRegister(), target);
          }
        }
      }
    }
    for (Map.Entry<VirtualRegister, StackSlot> entry : spills.entrySet()) {
      result.put(entry.getKey(), new StackLocation(entry.getValue()));
    }
    return result;
  }

  private static void ensureSlot(
      MachineFunction function,
      Map<VirtualRegister, StackSlot> spills,
      VirtualRegister register,
      RISCVTarget target) {
    if (spills.containsKey(register)) return;
    StackSlot slot =
        function.getFrameInfo().createSpillSlot(
            register.getType(), target.stackSizeOf(register.getType()), target.stackAlignOf(register.getType()));
    spills.put(register, slot);
  }
}
