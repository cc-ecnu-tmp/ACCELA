package accela.backend.lowering;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Fuses a single-use integer comparison into its immediately following branch. */
public final class CompareBranchFusion {
  public boolean run(MachineFunction function) {
    Map<VirtualRegister, Integer> useCounts = countUses(function);
    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      List<MachineInstr> instructions = block.getInstructions();
      for (int i = 0; i + 1 < instructions.size(); i++) {
        MachineInstr compare = instructions.get(i);
        MachineInstr branch = instructions.get(i + 1);
        if (!canFuse(compare, branch, useCounts)) continue;
        branch.setPredicate(compare.getPredicate());
        branch.setOperand(0, compare.getOperands().get(0));
        branch.addOperand(compare.getOperands().get(1));
        instructions.remove(i);
        changed = true;
      }
    }
    return changed;
  }

  private static Map<VirtualRegister, Integer> countUses(MachineFunction function) {
    Map<VirtualRegister, Integer> counts = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (var operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) {
            counts.merge(register.getRegister(), 1, Integer::sum);
          }
        }
      }
    }
    return counts;
  }

  private static boolean canFuse(
      MachineInstr compare, MachineInstr branch, Map<VirtualRegister, Integer> useCounts) {
    if (compare.getOpcode() != MachineOpcode.ICMP
        || branch.getOpcode() != MachineOpcode.CONDBR
        || !(branch.getOperands().get(0) instanceof VRegOperand condition)) return false;
    VirtualRegister result = compare.getDest();
    return condition.getRegister() == result && useCounts.getOrDefault(result, 0) == 1;
  }
}
