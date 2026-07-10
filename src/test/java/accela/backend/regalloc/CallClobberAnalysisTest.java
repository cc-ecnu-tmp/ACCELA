package accela.backend.regalloc;

import static org.junit.jupiter.api.Assertions.assertEquals;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.Set;
import org.junit.jupiter.api.Test;

final class CallClobberAnalysisTest {
  @Test
  void onlyConflictsValuesLiveAcrossCall() {
    MachineFunction function = new MachineFunction("call", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister survivor = function.createVirtualRegister(MachineType.I32, "survivor");
    VirtualRegister argument = function.createVirtualRegister(MachineType.I32, "argument");
    VirtualRegister result = function.createVirtualRegister(MachineType.I32, "result");
    VirtualRegister sum = function.createVirtualRegister(MachineType.I32, "sum");

    entry.addInstruction(argumentIn(survivor));
    entry.addInstruction(argumentIn(argument));

    MachineInstr call = new MachineInstr(MachineOpcode.CALL, result);
    call.addOperand(new VRegOperand(argument));
    call.setType(MachineType.I32);
    call.setCallee("callee");
    entry.addInstruction(call);

    MachineInstr add = new MachineInstr(MachineOpcode.ADD, sum);
    add.addOperand(new VRegOperand(survivor));
    add.addOperand(new VRegOperand(result));
    add.setType(MachineType.I32);
    entry.addInstruction(add);

    MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
    ret.addOperand(new VRegOperand(sum));
    ret.setType(MachineType.I32);
    entry.addInstruction(ret);

    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    assertEquals(
        Set.of(survivor), CallClobberAnalysis.analyze(function, liveness, new RISCVTarget()));
  }

  private static MachineInstr argumentIn(VirtualRegister register) {
    MachineInstr instruction = new MachineInstr(MachineOpcode.ARG_IN, register);
    instruction.setType(register.getType());
    return instruction;
  }
}
