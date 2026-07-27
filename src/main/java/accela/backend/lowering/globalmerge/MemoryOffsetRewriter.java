package accela.backend.lowering.globalmerge;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.List;

/** Redirects memory uses of an address while adjusting their encoded displacement. */
final class MemoryOffsetRewriter {
  private MemoryOffsetRewriter() {}

  static boolean rewrite(
      MachineFunction function,
      VirtualRegister removed,
      VirtualRegister replacement,
      int displacement) {
    List<Use> uses = new ArrayList<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (!(instruction.getOperands().get(index) instanceof VRegOperand operand)
              || !operand.getRegister().equals(removed)) continue;
          int addressIndex = instruction.getOpcode() == MachineOpcode.LOAD ? 0
              : instruction.getOpcode() == MachineOpcode.STORE ? 1 : -1;
          int offsetIndex = addressIndex + 1;
          if (index != addressIndex) return false;
          long oldOffset = instruction.getOperands().size() > offsetIndex
              ? ((ImmOperand) instruction.getOperands().get(offsetIndex)).getValue() : 0;
          long newOffset = oldOffset + displacement;
          if (newOffset < -2048 || newOffset > 2047) return false;
          uses.add(new Use(instruction, addressIndex, offsetIndex, newOffset));
        }
      }
    }
    for (Use use : uses) {
      use.instruction().setOperand(use.addressIndex(), new VRegOperand(replacement));
      ImmOperand offset = new ImmOperand(use.offset());
      if (use.instruction().getOperands().size() == use.offsetIndex()) {
        use.instruction().addOperand(offset);
      } else {
        use.instruction().setOperand(use.offsetIndex(), offset);
      }
    }
    return true;
  }

  private record Use(
      MachineInstr instruction, int addressIndex, int offsetIndex, long offset) {}
}
