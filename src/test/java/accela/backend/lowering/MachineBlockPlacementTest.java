package accela.backend.lowering;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.List;
import org.junit.jupiter.api.Test;

final class MachineBlockPlacementTest {
  @Test
  void placesTheLoopBodyBeforeItsExit() {
    MachineFunction function = new MachineFunction("loop", MachineType.VOID);
    MachineBasicBlock entry = function.addBlock("entry");
    MachineBasicBlock header = function.addBlock("header");
    MachineBasicBlock exit = function.addBlock("exit");
    MachineBasicBlock body = function.addBlock("body");
    VirtualRegister condition = function.createVirtualRegister(MachineType.I1, "condition");
    addBranch(entry, header);
    MachineInstr conditional = new MachineInstr(MachineOpcode.CONDBR, null);
    conditional.addOperand(new VRegOperand(condition));
    conditional.addOperand(new BlockOperand(body));
    conditional.addOperand(new BlockOperand(exit));
    header.addInstruction(conditional);
    exit.addInstruction(new MachineInstr(MachineOpcode.RET, null));
    addBranch(body, header);

    assertTrue(new MachineBlockPlacement().run(function));

    assertEquals(List.of(entry, header, body, exit), function.getBlocks());
  }

  private static void addBranch(MachineBasicBlock from, MachineBasicBlock to) {
    MachineInstr branch = new MachineInstr(MachineOpcode.BR, null);
    branch.addOperand(new BlockOperand(to));
    from.addInstruction(branch);
  }
}
