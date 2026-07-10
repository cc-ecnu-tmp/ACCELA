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

/** Forms simple CFG traces so common successors can fall through. */
public final class MachineBlockPlacement {
  public boolean run(MachineFunction function) {
    List<MachineBasicBlock> original = function.getBlocks();
    if (original.size() < 2) return false;
    List<MachineBasicBlock> order = new ArrayList<>(original.size());
    Set<MachineBasicBlock> placed =
        Collections.newSetFromMap(new IdentityHashMap<>());
    MachineBasicBlock current = function.getEntryBlock();
    while (order.size() < original.size()) {
      order.add(current);
      placed.add(current);
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
    for (MachineOperand operand : block.getInstructions().getLast().getOperands()) {
      if (operand instanceof BlockOperand target && !result.contains(target.getBlock())) {
        result.add(target.getBlock());
      }
    }
    return result;
  }
}
