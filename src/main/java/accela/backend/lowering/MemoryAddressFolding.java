package accela.backend.lowering;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Folds single-use pointer additions into RISC-V memory offsets. */
public final class MemoryAddressFolding {
  public boolean run(MachineFunction function) {
    Map<VirtualRegister, Use> uniqueUses = collectUniqueUses(function);
    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr address : List.copyOf(block.getInstructions())) {
        Fold fold = match(address, uniqueUses.get(address.getDest()));
        if (fold == null) continue;
        fold.use().instruction().setOperand(fold.use().operandIndex(), fold.base());
        fold.use().instruction().addOperand(new ImmOperand(fold.offset()));
        block.getInstructions().remove(address);
        changed = true;
      }
    }
    return changed;
  }

  private static Fold match(MachineInstr address, Use use) {
    if (address.getOpcode() != MachineOpcode.ADD
        || address.getType() != MachineType.PTR
        || use == null || use.multiple()) return null;
    MachineInstr memory = use.instruction();
    boolean load = memory.getOpcode() == MachineOpcode.LOAD
        && use.operandIndex() == 0 && memory.getOperands().size() == 1;
    boolean store = memory.getOpcode() == MachineOpcode.STORE
        && use.operandIndex() == 1 && memory.getOperands().size() == 2;
    if (!load && !store) return null;
    MachineOperand left = address.getOperands().get(0);
    MachineOperand right = address.getOperands().get(1);
    ImmOperand immediate = right instanceof ImmOperand value ? value
        : left instanceof ImmOperand value ? value : null;
    if (immediate == null || immediate.getValue() < -2048
        || immediate.getValue() > 2047) return null;
    return new Fold(right == immediate ? left : right, immediate.getValue(), use);
  }

  private static Map<VirtualRegister, Use> collectUniqueUses(MachineFunction function) {
    Map<VirtualRegister, Use> uses = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (!(instruction.getOperands().get(index) instanceof VRegOperand operand)) continue;
          VirtualRegister register = operand.getRegister();
          Use previous = uses.putIfAbsent(register, new Use(instruction, index, false));
          if (previous != null) uses.put(register, new Use(previous.instruction(), -1, true));
        }
      }
    }
    return uses;
  }

  private record Use(MachineInstr instruction, int operandIndex, boolean multiple) {}
  private record Fold(MachineOperand base, long offset, Use use) {}
}
