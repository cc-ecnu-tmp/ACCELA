package accela.backend.target;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import org.junit.jupiter.api.Test;

final class RISCVConstantDivisionLoweringTest {
  @Test
  void expandsRemaindersAndPoolsTheirConstants() {
    MachineFunction function = new MachineFunction("remainders", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister source = function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister first = function.createVirtualRegister(MachineType.I32, "first");
    VirtualRegister second = function.createVirtualRegister(MachineType.I32, "second");
    MachineInstr argument = new MachineInstr(MachineOpcode.ARG_IN, source);
    argument.setType(MachineType.I32);
    argument.addOperand(new ImmOperand(0));
    entry.addInstruction(argument);
    addRemainder(entry, first, source, 11);
    addRemainder(entry, second, source, 11);
    MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
    ret.setType(MachineType.I32);
    ret.addOperand(new VRegOperand(second));
    entry.addInstruction(ret);

    assertTrue(new RISCVConstantDivisionLowering().run(function));

    assertEquals(2, entry.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == MachineOpcode.CONST_INT).count());
    assertEquals(4, entry.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == MachineOpcode.ASHR).count());
    assertTrue(entry.getInstructions().stream()
        .noneMatch(instruction -> instruction.getOpcode() == MachineOpcode.REM));
    assertEquals(MachineOpcode.ARG_IN, entry.getInstructions().getFirst().getOpcode());
  }

  private static void addRemainder(
      MachineBasicBlock block, VirtualRegister result,
      VirtualRegister source, long divisor) {
    MachineInstr remainder = new MachineInstr(MachineOpcode.REM, result);
    remainder.setType(MachineType.I32);
    remainder.addOperand(new VRegOperand(source));
    remainder.addOperand(new ImmOperand(divisor));
    block.addInstruction(remainder);
  }
}
