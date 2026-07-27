package accela.backend.lowering;

import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.List;

/** Propagates register copies while freshly lowered Machine IR is still in SSA form. */
public final class MachineCopyPropagation {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (var block : function.getBlocks()) {
      for (MachineInstr copy : List.copyOf(block.getInstructions())) {
        VirtualRegister source = copySource(copy);
        if (source == null) continue;
        replaceUses(function, copy.getDest(), source);
        block.getInstructions().remove(copy);
        changed = true;
      }
    }
    return changed;
  }

  private static VirtualRegister copySource(MachineInstr instruction) {
    if (!instruction.isCoalescable()
        || instruction.getDest() == null
        || instruction.getOperands().size() != 1
        || !(instruction.getOperands().getFirst() instanceof VRegOperand operand)) {
      return null;
    }
    VirtualRegister source = operand.getRegister();
    MachineType destinationType = instruction.getDest().getType();
    boolean noOp = switch (instruction.getOpcode()) {
      case MOVE -> source.getType() == destinationType;
      case ZEXT -> source.getType() == MachineType.I1 && destinationType.isIntegerLike();
      // RV64 word operations and loads already sign-extend i32 values.
      case SEXT -> source.getType() == MachineType.I32 && destinationType == MachineType.I64;
      default -> false;
    };
    return noOp ? source : null;
  }

  private static void replaceUses(
      MachineFunction function, VirtualRegister copy, VirtualRegister source) {
    for (var block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (instruction.getOperands().get(index) instanceof VRegOperand operand
              && operand.getRegister().equals(copy)) {
            instruction.setOperand(index, new VRegOperand(source));
          }
        }
      }
    }
  }
}
