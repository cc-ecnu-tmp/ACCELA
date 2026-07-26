package accela.backend.lowering;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Shares large ADD immediates that occur at least three times in one machine block. */
public final class MachineConstantCSE {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      for (var entry : collectUses(block).entrySet()) {
        List<Use> uses = entry.getValue();
        if (uses.size() < 3) continue;
        materialize(function, block, entry.getKey(), uses);
        changed = true;
      }
    }
    return changed;
  }

  private static Map<Key, List<Use>> collectUses(MachineBasicBlock block) {
    Map<Key, List<Use>> uses = new LinkedHashMap<>();
    for (MachineInstr instruction : block.getInstructions()) {
      if (instruction.getOpcode() != MachineOpcode.ADD) continue;
      for (int index = 0; index < instruction.getOperands().size(); index++) {
        if (instruction.getOperands().get(index) instanceof ImmOperand immediate
            && !fitsSigned12(immediate.getValue())) {
          Key key = new Key(instruction.getType(), immediate.getValue());
          uses.computeIfAbsent(key, ignored -> new ArrayList<>())
              .add(new Use(instruction, index));
        }
      }
    }
    return uses;
  }

  private static void materialize(
      MachineFunction function, MachineBasicBlock block, Key key, List<Use> uses) {
    VirtualRegister register = function.createVirtualRegister(key.type(), "constant");
    MachineInstr constant = new MachineInstr(MachineOpcode.CONST_INT, register);
    constant.setType(key.type());
    constant.addOperand(new ImmOperand(key.value()));

    MachineInstr firstUse = uses.getFirst().instruction();
    block.getInstructions().add(block.getInstructions().indexOf(firstUse), constant);
    for (Use use : uses) {
      use.instruction().setOperand(use.index(), new VRegOperand(register));
    }
  }

  private static boolean fitsSigned12(long value) {
    return value >= -2048 && value <= 2047;
  }

  private record Key(MachineType type, long value) {}
  private record Use(MachineInstr instruction, int index) {}
}
