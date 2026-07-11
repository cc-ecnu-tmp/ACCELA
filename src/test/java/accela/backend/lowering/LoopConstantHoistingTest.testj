package accela.backend.lowering;

import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import org.junit.jupiter.api.Test;

final class LoopConstantHoistingTest {
  @Test
  void hoistsReusedConstantBeforeRotatedLoop() {
    MachineFunction function = new MachineFunction("loop", MachineType.VOID);
    MachineBasicBlock entry = function.addBlock("entry");
    MachineBasicBlock body = function.addBlock("body");
    MachineBasicBlock exit = function.addBlock("exit");
    addBranch(entry, body);
    VirtualRegister source = function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister constant = function.createVirtualRegister(MachineType.I32, "constant");
    VirtualRegister first = function.createVirtualRegister(MachineType.I32, "first");
    VirtualRegister second = function.createVirtualRegister(MachineType.I32, "second");
    MachineInstr materialize = new MachineInstr(MachineOpcode.CONST_INT, constant);
    materialize.setType(MachineType.I32);
    materialize.addOperand(new ImmOperand(17));
    body.addInstruction(materialize);
    add(body, first, source, constant);
    add(body, second, first, constant);
    MachineInstr branch = new MachineInstr(MachineOpcode.CONDBR, null);
    branch.addOperand(new VRegOperand(source));
    branch.addOperand(new BlockOperand(body));
    branch.addOperand(new BlockOperand(exit));
    body.addInstruction(branch);
    exit.addInstruction(new MachineInstr(MachineOpcode.RET, null));

    assertTrue(new LoopConstantHoisting().run(function));

    assertSame(materialize, entry.getInstructions().getFirst());
    assertTrue(!body.getInstructions().contains(materialize));
  }

  private static void add(
      MachineBasicBlock block, VirtualRegister result,
      VirtualRegister left, VirtualRegister right) {
    MachineInstr add = new MachineInstr(MachineOpcode.ADD, result);
    add.setType(MachineType.I32);
    add.addOperand(new VRegOperand(left)).addOperand(new VRegOperand(right));
    block.addInstruction(add);
  }

  private static void addBranch(MachineBasicBlock from, MachineBasicBlock to) {
    MachineInstr branch = new MachineInstr(MachineOpcode.BR, null);
    branch.addOperand(new BlockOperand(to));
    from.addInstruction(branch);
  }
}
