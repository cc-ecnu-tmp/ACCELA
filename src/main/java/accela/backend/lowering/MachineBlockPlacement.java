package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineOperand;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;

/**
 * Forms simple CFG traces so likely successors are emitted as fallthrough blocks.
 *
 * E.g. laying out `header, body, exit` lets a loop enter `body` without an
 * extra jump and lets its rotated backedge branch directly to `body`.
 */
public final class MachineBlockPlacement {
  public boolean run(MachineFunction function) {
    List<MachineBasicBlock> original = function.getBlocks();
    if (original.size() < 2) return false;

    List<MachineBasicBlock> order = new ArrayList<>(original.size());
    Set<MachineBasicBlock> placed = Collections.newSetFromMap(new IdentityHashMap<>());
    MachineBasicBlock current = function.getEntryBlock();
    while (order.size() < original.size()) {
      order.add(current);
      placed.add(current);

      // Follow the first unplaced CFG successor to extend the current trace. When it ends,
      // begin another trace at the earliest block in the original order.
      current = firstUnplaced(successors(current), placed);
      if (current == null) current = firstUnplaced(original, placed);
    }
    if (order.equals(original)) return false;
    function.reorderBlocks(order);
    return true;
  }

  private static MachineBasicBlock firstUnplaced(
      List<MachineBasicBlock> candidates, Set<MachineBasicBlock> placed) {
    for (MachineBasicBlock candidate : candidates) {
      if (!placed.contains(candidate)) return candidate;
    }
    return null;
  }

  private static List<MachineBasicBlock> successors(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return List.of();
    List<MachineBasicBlock> result = new ArrayList<>();

    // Block operands retain branch preference order (true before false for CONDBR).
    for (MachineOperand operand : block.getInstructions().getLast().getOperands()) {
      if (operand instanceof BlockOperand target && !result.contains(target.getBlock())) {
        result.add(target.getBlock());
      }
    }
    return result;
  }
}
