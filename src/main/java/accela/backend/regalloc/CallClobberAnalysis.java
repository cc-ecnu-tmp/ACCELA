package accela.backend.regalloc;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Finds values that survive an instruction which clobbers caller-saved registers. */
final class CallClobberAnalysis {
  private CallClobberAnalysis() {}

  static Set<VirtualRegister> analyze(
      MachineFunction function, LivenessAnalysis.Result liveness, RISCVTarget target) {
    Set<VirtualRegister> conflicts =
        Collections.newSetFromMap(new IdentityHashMap<>());
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        if (!clobbersCallerSaved(instruction, target)) continue;
        Set<VirtualRegister> liveAfter = liveness.liveAfter(instruction);
        for (VirtualRegister register : liveness.liveBefore(instruction)) {
          if (liveAfter.contains(register)) conflicts.add(register);
        }
      }
    }
    return conflicts;
  }

  private static boolean clobbersCallerSaved(MachineInstr instruction, RISCVTarget target) {
    if (instruction.getOpcode() == MachineOpcode.CALL) return true;
    if (instruction.getOpcode() != MachineOpcode.MEMZERO) return false;
    return instruction.getOperands().get(1) instanceof ImmOperand size
        && target.shouldUseMemsetLibcall((int) size.getValue());
  }
}
