package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;

/** Duplicates a side-effect-free loop header condition onto canonical backedges. */
public final class LoopConditionDuplication {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (MachineBasicBlock header : List.copyOf(function.getBlocks())) {
      if (header.getInstructions().size() != 1) continue;
      MachineInstr condition = header.getInstructions().get(0);
      if (condition.getOpcode() != MachineOpcode.CONDBR) continue;
      for (MachineBasicBlock block : function.getBlocks()) {
        if (!branchesTo(block, header) || !canReach(header, block)) continue;
        List<MachineInstr> instructions = block.getInstructions();
        instructions.set(instructions.size() - 1, copy(condition));
        changed = true;
      }
    }
    return changed;
  }

  private static boolean branchesTo(MachineBasicBlock block, MachineBasicBlock target) {
    if (block.getInstructions().isEmpty()) return false;
    MachineInstr terminator = block.getInstructions().getLast();
    return terminator.getOpcode() == MachineOpcode.BR
        && ((BlockOperand) terminator.getOperands().get(0)).getBlock() == target;
  }

  private static boolean canReach(MachineBasicBlock start, MachineBasicBlock target) {
    ArrayDeque<MachineBasicBlock> worklist = new ArrayDeque<>();
    Set<MachineBasicBlock> visited =
        Collections.newSetFromMap(new IdentityHashMap<>());
    worklist.add(start);
    while (!worklist.isEmpty()) {
      MachineBasicBlock block = worklist.removeFirst();
      if (!visited.add(block)) continue;
      if (block == target) return true;
      if (block.getInstructions().isEmpty()) continue;
      for (MachineOperand operand : block.getInstructions().getLast().getOperands()) {
        if (operand instanceof BlockOperand successor) worklist.add(successor.getBlock());
      }
    }
    return false;
  }

  private static MachineInstr copy(MachineInstr instruction) {
    MachineInstr result = new MachineInstr(MachineOpcode.CONDBR, null);
    for (MachineOperand operand : instruction.getOperands()) result.addOperand(operand);
    result.setPredicate(instruction.getPredicate());
    result.setType(instruction.getType());
    return result;
  }
}
