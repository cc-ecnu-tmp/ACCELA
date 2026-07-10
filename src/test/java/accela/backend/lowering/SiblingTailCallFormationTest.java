package accela.backend.lowering;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import org.junit.jupiter.api.Test;

final class SiblingTailCallFormationTest {
  @Test
  void formsTailCallWhenReturnUsesCallResult() {
    MachineFunction function = new MachineFunction("caller", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister argument = function.createVirtualRegister(MachineType.I32, "argument");
    VirtualRegister result = function.createVirtualRegister(MachineType.I32, "result");
    MachineInstr call = new MachineInstr(MachineOpcode.CALL, result);
    call.setCallee("callee");
    call.setType(MachineType.I32);
    call.addOperand(new VRegOperand(argument));
    entry.addInstruction(call);
    MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
    ret.setType(MachineType.I32);
    ret.addOperand(new VRegOperand(result));
    entry.addInstruction(ret);

    assertTrue(new SiblingTailCallFormation().run(function));

    MachineInstr tail = entry.getInstructions().getLast();
    assertEquals(MachineOpcode.TAILCALL, tail.getOpcode());
    assertEquals("callee", tail.getCallee());
    assertEquals(1, tail.getOperands().size());
    assertTrue(tail.isTerminator());
  }
}
