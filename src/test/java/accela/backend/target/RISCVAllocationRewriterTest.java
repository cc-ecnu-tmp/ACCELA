package accela.backend.target;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.frame.StackSlot;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.StackLocation;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

final class RISCVAllocationRewriterTest {
  @Test
  void omitsMoveBetweenTheSameSpillSlot() {
    Fixture fixture = new Fixture(true);

    fixture.emitMove();

    assertTrue(fixture.lines.isEmpty());
  }

  @Test
  void keepsMoveBetweenDifferentSpillSlots() {
    Fixture fixture = new Fixture(false);

    fixture.emitMove();

    assertFalse(fixture.lines.isEmpty());
  }

  private static final class Fixture {
    final RISCVTarget target = new RISCVTarget();
    final RISCVFrameLowering frame = new RISCVFrameLowering(target);
    final RISCVAllocationRewriter rewriter = new RISCVAllocationRewriter(target, frame);
    final MachineFunction function = new MachineFunction("move", MachineType.I32);
    final AllocationResult allocation = new AllocationResult();
    final VirtualRegister source = function.createVirtualRegister(MachineType.I32, "source");
    final VirtualRegister destination = function.createVirtualRegister(MachineType.I32, "destination");
    final List<String> lines = new ArrayList<>();

    Fixture(boolean sameSlot) {
      StackSlot sourceSlot = function.getFrameInfo().createSpillSlot(MachineType.I32, 4, 4);
      StackSlot destinationSlot = sameSlot
          ? sourceSlot
          : function.getFrameInfo().createSpillSlot(MachineType.I32, 4, 4);
      allocation.put(source, new StackLocation(sourceSlot));
      allocation.put(destination, new StackLocation(destinationSlot));
      frame.finalizeFrame(function);
    }

    void emitMove() {
      MachineInstr move = new MachineInstr(MachineOpcode.MOVE, destination);
      move.addOperand(new VRegOperand(source));
      move.setType(MachineType.I32);
      rewriter.emitInstruction(function, null, move, allocation, lines);
    }
  }
}
