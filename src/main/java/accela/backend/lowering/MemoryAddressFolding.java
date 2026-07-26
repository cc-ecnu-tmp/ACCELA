package accela.backend.lowering;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Folds address = base + offset; load address into load offset(base).
 *
 * The address must have one use so its ADD and virtual register can both disappear before
 * register allocation.
 */
public final class MemoryAddressFolding {
  public boolean run(MachineFunction function) {
    Map<VirtualRegister, List<Use>> uses = collectUses(function);
    boolean changed = false;
    for (var block : function.getBlocks()) {
      for (MachineInstr address : List.copyOf(block.getInstructions())) {
        Use use = singleUse(uses.get(address.getDest()));
        BaseOffset folded = matchAddress(address);
        if (use == null || folded == null || !isMemoryAddress(use)) continue;

        use.instruction().setOperand(use.index(), folded.base());
        use.instruction().addOperand(folded.offset());
        block.getInstructions().remove(address);
        changed = true;
      }
    }
    return changed;
  }

  private static Use singleUse(List<Use> uses) {
    return uses != null && uses.size() == 1 ? uses.getFirst() : null;
  }

  private static BaseOffset matchAddress(MachineInstr address) {
    if (address.getOpcode() != MachineOpcode.ADD || address.getType() != MachineType.PTR) return null;
    MachineOperand left = address.getOperands().get(0);
    MachineOperand right = address.getOperands().get(1);
    ImmOperand offset = right instanceof ImmOperand value ? value
        : left instanceof ImmOperand value ? value : null;
    // RISC-V loads and stores encode a signed 12-bit byte offset.
    if (offset == null || offset.getValue() < -2048 || offset.getValue() > 2047) return null;
    return new BaseOffset(right == offset ? left : right, offset);
  }

  private static boolean isMemoryAddress(Use use) {
    MachineInstr instruction = use.instruction();
    return switch (instruction.getOpcode()) {
      case LOAD -> use.index() == 0 && instruction.getOperands().size() == 1;
      case STORE -> use.index() == 1 && instruction.getOperands().size() == 2;
      default -> false;
    };
  }

  private static Map<VirtualRegister, List<Use>> collectUses(MachineFunction function) {
    Map<VirtualRegister, List<Use>> uses = new HashMap<>();
    for (var block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (instruction.getOperands().get(index) instanceof VRegOperand operand) {
            uses.computeIfAbsent(operand.getRegister(), ignored -> new ArrayList<>())
                .add(new Use(instruction, index));
          }
        }
      }
    }
    return uses;
  }

  private record BaseOffset(MachineOperand base, ImmOperand offset) {}
  private record Use(MachineInstr instruction, int index) {}
}
