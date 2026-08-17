package accela.backend.lowering;

import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.RVVConfig;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * Materializes the implicit vl/vtype dependency before register allocation.
 *
 * <p>Block entries conservatively start with unknown state. Within a block identical settings are
 * coalesced; calls and explicit unknown state changes invalidate the tracked configuration.
 */
public final class RVVConfigInsertion {
  public void run(accela.backend.machine.MachineFunction function) {
    for (var block : function.getBlocks()) {
      RVVConfig current = null;
      List<MachineInstr> rewritten = new ArrayList<>();
      for (MachineInstr instruction : block.getInstructions()) {
        RVVConfig required = instruction.getRVVConfig();
        if (required != null && instruction.getOpcode() != MachineOpcode.VSET
            && !Objects.equals(current, required)) {
          MachineInstr set = new MachineInstr(MachineOpcode.VSET, null);
          set.setRVVConfig(required);
          rewritten.add(set);
          current = required;
        }
        rewritten.add(instruction);
        if (instruction.getOpcode() == MachineOpcode.CALL) current = null;
        else if (instruction.getOpcode() == MachineOpcode.VSET) current = instruction.getRVVConfig();
      }
      block.getInstructions().clear();
      block.getInstructions().addAll(rewritten);
    }
  }
}
