package accela.backend.lowering;

import accela.backend.machine.FloatImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import java.util.List;

/** Forms register-only sibling tail calls from call-return pairs. */
public final class SiblingTailCallFormation {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      List<MachineInstr> instructions = block.getInstructions();
      if (instructions.size() < 2) continue;
      MachineInstr call = instructions.get(instructions.size() - 2);
      MachineInstr ret = instructions.getLast();
      if (!canForm(call, ret)) continue;
      MachineInstr tail = new MachineInstr(MachineOpcode.TAILCALL, null);
      tail.setCallee(call.getCallee());
      tail.setType(call.getType());
      for (var operand : call.getOperands()) tail.addOperand(operand);
      instructions.set(instructions.size() - 2, tail);
      instructions.removeLast();
      changed = true;
    }
    return changed;
  }

  private static boolean canForm(MachineInstr call, MachineInstr ret) {
    if (call.getOpcode() != MachineOpcode.CALL
        || ret.getOpcode() != MachineOpcode.RET
        || call.getOperands().size() > 8
        || call.getOperands().stream().anyMatch(SiblingTailCallFormation::isFloat)) {
      return false;
    }
    if (call.getDest() == null) return ret.getOperands().isEmpty();
    return ret.getOperands().size() == 1
        && ret.getOperands().getFirst() instanceof VRegOperand result
        && result.getRegister() == call.getDest();
  }

  private static boolean isFloat(accela.backend.machine.MachineOperand operand) {
    return operand instanceof FloatImmOperand
        || operand instanceof VRegOperand register
            && register.getRegister().getType().isFloat();
  }
}
