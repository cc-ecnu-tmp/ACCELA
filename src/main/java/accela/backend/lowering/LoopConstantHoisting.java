package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.List;

/** Hoists reused constants from rotated self-loops into their unique entry. */
public final class LoopConstantHoisting {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (MachineBasicBlock loop : function.getBlocks()) {
      if (!branchesTo(loop, loop)) continue;
      List<MachineBasicBlock> entries = predecessors(function, loop).stream()
          .filter(predecessor -> predecessor != loop).toList();
      if (entries.size() != 1) continue;
      MachineBasicBlock preheader = entries.getFirst();
      for (MachineInstr instruction : List.copyOf(loop.getInstructions())) {
        if (instruction.getOpcode() != MachineOpcode.CONST_INT
            || usesIn(instruction.getDest(), loop) < 2) continue;
        loop.getInstructions().remove(instruction);
        preheader.insertBeforeTerminator(instruction);
        changed = true;
      }
    }
    return changed;
  }

  private static int usesIn(VirtualRegister register, MachineBasicBlock block) {
    int uses = 0;
    for (MachineInstr instruction : block.getInstructions()) {
      for (var operand : instruction.getOperands()) {
        if (operand instanceof VRegOperand value && value.getRegister() == register) uses++;
      }
    }
    return uses;
  }

  private static List<MachineBasicBlock> predecessors(
      MachineFunction function, MachineBasicBlock target) {
    List<MachineBasicBlock> result = new ArrayList<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      if (branchesTo(block, target)) result.add(block);
    }
    return result;
  }

  private static boolean branchesTo(MachineBasicBlock block, MachineBasicBlock target) {
    if (block.getInstructions().isEmpty()) return false;
    for (var operand : block.getInstructions().getLast().getOperands()) {
      if (operand instanceof BlockOperand destination
          && destination.getBlock() == target) return true;
    }
    return false;
  }
}
