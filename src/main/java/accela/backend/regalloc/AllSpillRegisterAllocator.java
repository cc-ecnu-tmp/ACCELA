package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.LinkedHashMap;
import java.util.Map;

public final class AllSpillRegisterAllocator implements RegisterAllocator {
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
