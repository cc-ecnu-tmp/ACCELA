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

/**
 * Rotates canonical loops by copying an empty header's condition onto its backedges.
 *
 * preheader -> header: condbr body, exit    preheader -> header: condbr body, exit
 * body      -> header                  =>  body:      condbr body, exit
 */
public final class LoopConditionDuplication {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (MachineBasicBlock header : List.copyOf(function.getBlocks())) {
      if (header.getInstructions().size() != 1) continue;
      MachineInstr condition = header.getInstructions().getFirst();
      if (condition.getOpcode() != MachineOpcode.CONDBR) continue;

      // A predecessor reachable from the header is a backedge; an ordinary preheader is not.
      Set<MachineBasicBlock> reachable = reachableFrom(header);
      for (MachineBasicBlock block : function.getBlocks()) {
        if (!reachable.contains(block) || !branchesTo(block, header)) continue;

        // Recheck the loop condition directly instead of jumping through the empty header.
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
        && ((BlockOperand) terminator.getOperands().getFirst()).getBlock() == target;
  }

  private static Set<MachineBasicBlock> reachableFrom(MachineBasicBlock start) {
    ArrayDeque<MachineBasicBlock> worklist = new ArrayDeque<>();
    Set<MachineBasicBlock> visited = Collections.newSetFromMap(new IdentityHashMap<>());
    worklist.add(start);
    while (!worklist.isEmpty()) {
      MachineBasicBlock block = worklist.removeFirst();
      if (!visited.add(block)) continue;
      if (block.getInstructions().isEmpty()) continue;
      for (MachineOperand operand : block.getInstructions().getLast().getOperands()) {
        if (operand instanceof BlockOperand successor) worklist.add(successor.getBlock());
      }
    }
    return visited;
  }

  private static MachineInstr copy(MachineInstr instruction) {
    // MachineInstr is mutable, so each backedge needs its own branch object.
    MachineInstr result = new MachineInstr(MachineOpcode.CONDBR, null);
    for (MachineOperand operand : instruction.getOperands()) result.addOperand(operand);
    result.setPredicate(instruction.getPredicate());
    result.setType(instruction.getType());
    return result;
  }
}
