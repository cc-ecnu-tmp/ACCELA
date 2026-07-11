package accela.backend.regalloc;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import org.junit.jupiter.api.Test;

final class MachineLoopAnalysisTest {
  @Test
  void marksBlocksInCyclicComponents() {
    MachineFunction function = new MachineFunction("loop", MachineType.VOID);
    MachineBasicBlock entry = function.addBlock("entry");
    MachineBasicBlock header = function.addBlock("header");
    MachineBasicBlock body = function.addBlock("body");
    MachineBasicBlock exit = function.addBlock("exit");
    branch(entry, header);
    MachineInstr conditional = new MachineInstr(MachineOpcode.CONDBR, null);
    conditional.addOperand(new VRegOperand(
        function.createVirtualRegister(MachineType.I1, "condition")));
    conditional.addOperand(new BlockOperand(body));
    conditional.addOperand(new BlockOperand(exit));
    header.addInstruction(conditional);
    branch(body, header);
    exit.addInstruction(new MachineInstr(MachineOpcode.RET, null));

    var loopBlocks = MachineLoopAnalysis.findLoopBlocks(function);

    assertTrue(loopBlocks.contains(header));
    assertTrue(loopBlocks.contains(body));
    assertFalse(loopBlocks.contains(entry));
    assertFalse(loopBlocks.contains(exit));
  }

  private static void branch(MachineBasicBlock from, MachineBasicBlock to) {
    MachineInstr branch = new MachineInstr(MachineOpcode.BR, null);
    branch.addOperand(new BlockOperand(to));
    from.addInstruction(branch);
  }
}
