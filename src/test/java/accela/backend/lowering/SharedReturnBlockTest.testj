package accela.backend.lowering;

import static org.junit.jupiter.api.Assertions.assertEquals;
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
import java.util.List;
import org.junit.jupiter.api.Test;

final class SharedReturnBlockTest {
  @Test
  void funnelsReturnValuesThroughOneExit() {
    MachineFunction function = new MachineFunction("choose", MachineType.I32);
    MachineBasicBlock left = function.addBlock("left");
    MachineBasicBlock right = function.addBlock("right");
    addReturn(left, 1);
    addReturn(right, 2);

    assertTrue(new SharedReturnBlock().run(function));

    MachineBasicBlock exit = function.getBlocks().getLast();
    assertEquals(MachineOpcode.RET, exit.getInstructions().getFirst().getOpcode());
    var result = ((VRegOperand) exit.getInstructions().getFirst()
        .getOperands().getFirst()).getRegister();
    for (MachineBasicBlock predecessor : List.of(left, right)) {
      assertEquals(MachineOpcode.MOVE, predecessor.getInstructions().getFirst().getOpcode());
      assertSame(result, predecessor.getInstructions().getFirst().getDest());
      MachineInstr branch = predecessor.getInstructions().getLast();
      assertEquals(MachineOpcode.BR, branch.getOpcode());
      assertSame(exit, ((BlockOperand) branch.getOperands().getFirst()).getBlock());
    }
  }

  private static void addReturn(MachineBasicBlock block, long value) {
    MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
    ret.setType(MachineType.I32);
    ret.addOperand(new ImmOperand(value));
    block.addInstruction(ret);
  }
}
