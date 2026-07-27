package accela.backend.regalloc;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Splits call operands so their pre-call ranges can retain ABI-register affinity. */
final class LiveRangeSplitting {
  private LiveRangeSplitting() {}

  static boolean run(MachineFunction function, RISCVTarget target) {
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr call : List.copyOf(block.getInstructions())) {
        if (call.getOpcode() != MachineOpcode.CALL) continue;
        for (VirtualRegister register : splittableCallOperands(call, target)) {
          if (!liveness.liveAfter(call).contains(register)
              || liveness.liveOut(block).contains(register)
              || !hasUseAfter(block, call, register)) continue;
          splitAfterCall(function, block, call, register);
          changed = true;
        }
      }
    }
    return changed;
  }

  private static Set<VirtualRegister> splittableCallOperands(
      MachineInstr call, RISCVTarget target) {
    Map<VirtualRegister, String> assignments = new HashMap<>();
    Set<VirtualRegister> conflicts = new HashSet<>();
    RISCVTarget.CallArgCursor cursor = target.newCallArgCursor();
    for (int index = 0; index < call.getOperands().size(); index++) {
      MachineType type = call.getOperandType(index);
      RISCVTarget.CallArgAssignment assignment = target.assignCallArg(cursor, type);
      if (!assignment.isInRegister()
          || !(call.getOperands().get(index) instanceof VRegOperand operand)) continue;
      String previous =
          assignments.putIfAbsent(
              operand.getRegister(), assignment.getRegister().getName());
      if (previous != null && !previous.equals(assignment.getRegister().getName())) {
        conflicts.add(operand.getRegister());
      }
    }
    assignments.keySet().removeAll(conflicts);
    return assignments.keySet();
  }

  private static boolean hasUseAfter(
      MachineBasicBlock block, MachineInstr call, VirtualRegister register) {
    List<MachineInstr> instructions = block.getInstructions();
    for (int index = instructions.indexOf(call) + 1; index < instructions.size(); index++) {
      if (uses(instructions.get(index), register)) return true;
    }
    return false;
  }

  private static void splitAfterCall(
      MachineFunction function,
      MachineBasicBlock block,
      MachineInstr call,
      VirtualRegister register) {
    VirtualRegister split = function.createVirtualRegister(register.getType(), "call.split");
    MachineInstr copy = new MachineInstr(MachineOpcode.MOVE, split);
    copy.setType(register.getType());
    copy.setCoalescable(false);
    copy.addOperand(new VRegOperand(register));
    block.getInstructions().add(block.getInstructions().indexOf(call), copy);
    List<MachineInstr> instructions = block.getInstructions();
    for (int index = instructions.indexOf(call) + 1; index < instructions.size(); index++) {
      MachineInstr instruction = instructions.get(index);
      for (int operand = 0; operand < instruction.getOperands().size(); operand++) {
        if (instruction.getOperands().get(operand) instanceof VRegOperand value
            && value.getRegister().equals(register)) {
          instruction.setOperand(operand, new VRegOperand(split));
        }
      }
    }
  }

  private static boolean uses(MachineInstr instruction, VirtualRegister register) {
    return instruction.getOperands().stream()
        .filter(VRegOperand.class::isInstance)
        .map(VRegOperand.class::cast)
        .anyMatch(operand -> operand.getRegister().equals(register));
  }
}
