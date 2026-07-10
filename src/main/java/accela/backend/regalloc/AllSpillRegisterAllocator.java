package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class AllSpillRegisterAllocator implements RegisterAllocator {
  @Override
  public AllocationResult allocate(MachineFunction function, RISCVTarget target) {
    AllocationResult result = new AllocationResult();
    Set<VirtualRegister> registers = new LinkedHashSet<>();
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    InterferenceGraph interference = InterferenceGraph.build(function, liveness);
    collectRegisters(function, registers);

    Map<VirtualRegister, StackSlot> spills = new LinkedHashMap<>();
    Map<StackSlot, List<VirtualRegister>> occupants = new LinkedHashMap<>();
    for (VirtualRegister register : registers) {
      StackSlot slot = null;
      for (var entry : occupants.entrySet()) {
        List<VirtualRegister> assigned = entry.getValue();
        if (assigned.get(0).getType() == register.getType()
            && assigned.stream().noneMatch(other -> interference.interferes(register, other))) {
          slot = entry.getKey();
          break;
        }
      }
      if (slot == null) {
        slot = function.getFrameInfo().createSpillSlot(
            register.getType(), target.stackSizeOf(register.getType()),
            target.stackAlignOf(register.getType()));
        occupants.put(slot, new ArrayList<>());
      }
      occupants.get(slot).add(register);
      spills.put(register, slot);
    }
    for (Map.Entry<VirtualRegister, StackSlot> entry : spills.entrySet()) {
      result.put(entry.getKey(), new StackLocation(entry.getValue()));
    }
    return result;
  }

  private static void collectRegisters(
      MachineFunction function, Set<VirtualRegister> registers) {
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        if (instruction.getDest() != null) registers.add(instruction.getDest());
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) registers.add(register.getRegister());
        }
      }
    }
  }
}
