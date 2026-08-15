package accela.backend.machine;

import accela.backend.regalloc.AllocationResult;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Structural verifier for Machine IR after register allocation and CFG folding. */
public final class AllocatedMachineVerifier {
  private AllocatedMachineVerifier() {}

  public static void verify(MachineFunction function, AllocationResult allocation) {
    if (function == null || allocation == null) {
      throw new IllegalArgumentException("allocated Machine IR and allocation are required");
    }
    Set<MachineBasicBlock> blocks = Collections.newSetFromMap(new IdentityHashMap<>());
    if (function.getBlocks().isEmpty()) fail(function, "no basic blocks");
    for (MachineBasicBlock block : function.getBlocks()) {
      if (!blocks.add(block)) fail(function, "duplicate basic block object");
      if (block.getInstructions().isEmpty()) fail(function, "empty basic block");
      for (int index = 0; index < block.getInstructions().size(); index++) {
        MachineInstr instruction = block.getInstructions().get(index);
        if (isTerminator(instruction.getOpcode())
            && index != block.getInstructions().size() - 1) {
          fail(function, "terminator is not last in block " + block.getLabel());
        }
        requireAllocated(function, allocation, instruction.getDest());
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) {
            requireAllocated(function, allocation, register.getRegister());
          }
        }
      }
      if (!isTerminator(block.getInstructions().getLast().getOpcode())) {
        fail(function, "block has no terminator: " + block.getLabel());
      }
    }
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof BlockOperand target && !blocks.contains(target.getBlock())) {
            fail(function, "branch references a foreign basic block");
          }
        }
      }
    }
  }

  private static void requireAllocated(MachineFunction function,
      AllocationResult allocation, VirtualRegister register) {
    if (register != null && allocation.locationOf(register) == null) {
      fail(function, "virtual register has no allocation: " + register.getId());
    }
  }

  private static boolean isTerminator(MachineOpcode opcode) {
    return opcode == MachineOpcode.BR || opcode == MachineOpcode.CONDBR
        || opcode == MachineOpcode.RET;
  }

  private static void fail(MachineFunction function, String reason) {
    throw new IllegalStateException(
        "invalid allocated Machine IR: " + function.getName() + ": " + reason);
  }
}
