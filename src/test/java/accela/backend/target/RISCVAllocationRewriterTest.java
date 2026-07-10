package accela.backend.target;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.frame.StackSlot;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterLocation;
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

  @Test
  void storesDirectlyFromAllocatedRegisters() {
    Fixture fixture = new Fixture(true);
    VirtualRegister value = fixture.function.createVirtualRegister(MachineType.I32, "value");
    VirtualRegister address = fixture.function.createVirtualRegister(MachineType.PTR, "address");
    fixture.allocation.put(value,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(address,
        new RegisterLocation(new PhysicalRegister("t5", MachineType.PTR)));
    MachineInstr store = new MachineInstr(MachineOpcode.STORE, null);
    store.addOperand(new VRegOperand(value));
    store.addOperand(new VRegOperand(address));
    store.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, store, fixture.allocation, fixture.lines);

    assertEquals(List.of("  sw t4, 0(t5)"), fixture.lines);
  }

  @Test
  void loadsDirectlyFromAnAllocatedAddress() {
    Fixture fixture = new Fixture(true);
    VirtualRegister address = fixture.function.createVirtualRegister(MachineType.PTR, "address");
    VirtualRegister value = fixture.function.createVirtualRegister(MachineType.I32, "value");
    fixture.allocation.put(address,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.PTR)));
    fixture.allocation.put(value,
        new RegisterLocation(new PhysicalRegister("t5", MachineType.I32)));
    MachineInstr load = new MachineInstr(MachineOpcode.LOAD, value);
    load.addOperand(new VRegOperand(address));
    load.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, load, fixture.allocation, fixture.lines);

    assertEquals(List.of("  lw t5, 0(t4)"), fixture.lines);
  }

  @Test
  void convertsDirectlyFromAnAllocatedRegister() {
    Fixture fixture = new Fixture(true);
    VirtualRegister source = fixture.function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister result = fixture.function.createVirtualRegister(MachineType.F32, "result");
    fixture.allocation.put(source,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(result,
        new RegisterLocation(new PhysicalRegister("ft4", MachineType.F32)));
    MachineInstr convert = new MachineInstr(MachineOpcode.SITOFP, result);
    convert.addOperand(new VRegOperand(source));
    convert.setType(MachineType.F32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, convert, fixture.allocation, fixture.lines);

    assertEquals(List.of("  fcvt.s.w ft4, t4"), fixture.lines);
  }

  @Test
  void emitsImmediateIntegerAnd() {
    Fixture fixture = new Fixture(true);
    VirtualRegister source = fixture.function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister result = fixture.function.createVirtualRegister(MachineType.I32, "result");
    fixture.allocation.put(source,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(result,
        new RegisterLocation(new PhysicalRegister("t5", MachineType.I32)));
    MachineInstr and = new MachineInstr(MachineOpcode.AND, result);
    and.addOperand(new VRegOperand(source));
    and.addOperand(new ImmOperand(1));
    and.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, and, fixture.allocation, fixture.lines);

    assertEquals(List.of("  andi t5, t4, 1"), fixture.lines);
  }

  @Test
  void strengthReducesSignedPowerOfTwoDivision() {
    Fixture fixture = new Fixture(true);
    VirtualRegister source = fixture.function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister result = fixture.function.createVirtualRegister(MachineType.I32, "result");
    fixture.allocation.put(source,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(result,
        new RegisterLocation(new PhysicalRegister("t5", MachineType.I32)));
    MachineInstr divide = new MachineInstr(MachineOpcode.DIV, result);
    divide.addOperand(new VRegOperand(source));
    divide.addOperand(new ImmOperand(8));
    divide.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, divide, fixture.allocation, fixture.lines);

    assertEquals("  sraiw t5, t5, 3", fixture.lines.getLast());
    assertFalse(fixture.lines.stream().anyMatch(line -> line.contains("divw")));
  }

  @Test
  void strengthReducesSignedConstantDivision() {
    Fixture fixture = new Fixture(true);
    VirtualRegister source = fixture.function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister result = fixture.function.createVirtualRegister(MachineType.I32, "result");
    fixture.allocation.put(source,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(result,
        new RegisterLocation(new PhysicalRegister("t5", MachineType.I32)));
    MachineInstr divide = new MachineInstr(MachineOpcode.DIV, result);
    divide.addOperand(new VRegOperand(source));
    divide.addOperand(new ImmOperand(11));
    divide.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, divide, fixture.allocation, fixture.lines);

    assertTrue(fixture.lines.stream().anyMatch(line -> line.contains("mul t1")));
    assertFalse(fixture.lines.stream().anyMatch(line -> line.contains("divw")));
  }

  @Test
  void strengthReducesSignedConstantRemainder() {
    Fixture fixture = new Fixture(true);
    VirtualRegister source = fixture.function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister result = fixture.function.createVirtualRegister(MachineType.I32, "result");
    fixture.allocation.put(source,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(result,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    MachineInstr remainder = new MachineInstr(MachineOpcode.REM, result);
    remainder.addOperand(new VRegOperand(source));
    remainder.addOperand(new ImmOperand(11));
    remainder.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, remainder, fixture.allocation, fixture.lines);

    assertEquals("  mv t0, t4", fixture.lines.getFirst());
    assertEquals("  subw t4, t0, t1", fixture.lines.getLast());
    assertFalse(fixture.lines.stream().anyMatch(line -> line.contains("remw")));
  }

  @Test
  void strengthReducesTwoBitMultiplication() {
    Fixture fixture = new Fixture(true);
    VirtualRegister source = fixture.function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister result = fixture.function.createVirtualRegister(MachineType.I32, "result");
    fixture.allocation.put(source,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    fixture.allocation.put(result,
        new RegisterLocation(new PhysicalRegister("t4", MachineType.I32)));
    MachineInstr multiply = new MachineInstr(MachineOpcode.MUL, result);
    multiply.addOperand(new VRegOperand(source));
    multiply.addOperand(new ImmOperand(5));
    multiply.setType(MachineType.I32);

    fixture.rewriter.emitInstruction(
        fixture.function, null, multiply, fixture.allocation, fixture.lines);

    assertEquals("  addw t4, t4, t3", fixture.lines.getLast());
    assertFalse(fixture.lines.stream().anyMatch(line -> line.contains("mulw")));
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
