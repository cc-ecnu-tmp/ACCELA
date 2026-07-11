package accela.backend.lowering;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
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

final class MemoryAddressFoldingTest {
  @Test
  void foldsSingleUsePointerAdditionIntoLoad() {
    MachineFunction function = new MachineFunction("load", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister base = function.createVirtualRegister(MachineType.PTR, "base");
    VirtualRegister address = function.createVirtualRegister(MachineType.PTR, "address");
    VirtualRegister value = function.createVirtualRegister(MachineType.I32, "value");
    MachineInstr add = new MachineInstr(MachineOpcode.ADD, address);
    add.setType(MachineType.PTR);
    add.addOperand(new VRegOperand(base));
    add.addOperand(new ImmOperand(-1000));
    entry.addInstruction(add);
    MachineInstr load = new MachineInstr(MachineOpcode.LOAD, value);
    load.setType(MachineType.I32);
    load.addOperand(new VRegOperand(address));
    entry.addInstruction(load);
    MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
    ret.setType(MachineType.I32);
    ret.addOperand(new VRegOperand(value));
    entry.addInstruction(ret);

    assertTrue(new MemoryAddressFolding().run(function));

    assertTrue(!entry.getInstructions().contains(add));
    assertSame(base, ((VRegOperand) load.getOperands().get(0)).getRegister());
    assertEquals(-1000, ((ImmOperand) load.getOperands().get(1)).getValue());
  }
}
