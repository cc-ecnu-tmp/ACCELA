package accela.backend.regalloc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.FloatImmOperand;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import org.junit.jupiter.api.Test;

final class AllSpillRegisterAllocatorTest {
  @Test
  void assignsLiveAcrossCallToCalleeSavedRegister() {
    MachineFunction function = new MachineFunction("cross_call", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister source = function.createVirtualRegister(MachineType.I32, "source");
    addArg(entry, source);
    addCall(entry);
    addReturn(entry, source);
    RISCVTarget target = new RISCVTarget();

    AllocationResult allocation = new AllSpillRegisterAllocator().allocate(function, target);
    RegisterLocation location =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(source));

    assertTrue(target.isCalleeSaved(location.getRegister()));
    assertTrue(function.getFrameInfo().getCalleeSavedOffsets()
        .containsKey(location.getRegister().getName()));
  }

  @Test
  void assignsFloatLiveAcrossCallToCalleeSavedRegister() {
    MachineFunction function = new MachineFunction("float_cross_call", MachineType.F32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister source = function.createVirtualRegister(MachineType.F32, "source");
    addArg(entry, source);
    addCall(entry);
    addReturn(entry, source);
    RISCVTarget target = new RISCVTarget();

    AllocationResult allocation = new AllSpillRegisterAllocator().allocate(function, target);
    RegisterLocation location =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(source));

    assertTrue(target.isCalleeSaved(location.getRegister()));
    assertTrue(function.getFrameInfo().getFloatCalleeSavedOffsets()
        .containsKey(location.getRegister().getName()));
  }

  @Test
  void colorsInterferingValuesDifferently() {
    MachineFunction function = new MachineFunction("color", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister source = function.createVirtualRegister(MachineType.I32, "source");
    VirtualRegister first = function.createVirtualRegister(MachineType.I32, "first");
    VirtualRegister result = function.createVirtualRegister(MachineType.I32, "result");
    addArg(entry, source);
    add(entry, first, source, new ImmOperand(1));
    add(entry, result, source, new VRegOperand(first));
    addReturn(entry, result);

    AllocationResult allocation = new AllSpillRegisterAllocator().allocate(function, new RISCVTarget());
    RegisterLocation sourceLocation =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(source));
    RegisterLocation firstLocation =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(first));

    assertNotEquals(
        sourceLocation.getRegister().getName(), firstLocation.getRegister().getName());
  }

  @Test
  void reusesColorWhenLiveRangesOnlyTouchAtInstructionBoundary() {
    MachineFunction function = new MachineFunction("reuse", MachineType.F32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister source = function.createVirtualRegister(MachineType.F32, "source");
    VirtualRegister result = function.createVirtualRegister(MachineType.F32, "result");
    addArg(entry, source);
    addCall(entry);
    add(entry, result, source, new FloatImmOperand(1));
    addCall(entry);
    addReturn(entry, result);

    AllocationResult allocation = new AllSpillRegisterAllocator().allocate(function, new RISCVTarget());

    RegisterLocation sourceLocation =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(source));
    RegisterLocation resultLocation =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(result));
    assertEquals(sourceLocation.getRegister().getName(), resultLocation.getRegister().getName());
  }

  @Test
  void separatesSimultaneouslyLiveRegisters() {
    MachineFunction function = new MachineFunction("interfere", MachineType.F32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister source = function.createVirtualRegister(MachineType.F32, "source");
    VirtualRegister first = function.createVirtualRegister(MachineType.F32, "first");
    VirtualRegister result = function.createVirtualRegister(MachineType.F32, "result");
    addArg(entry, source);
    add(entry, first, source, new FloatImmOperand(1));
    addCall(entry);
    add(entry, result, source, new VRegOperand(first));
    addReturn(entry, result);

    AllocationResult allocation = new AllSpillRegisterAllocator().allocate(function, new RISCVTarget());

    RegisterLocation sourceLocation =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(source));
    RegisterLocation firstLocation =
        assertInstanceOf(RegisterLocation.class, allocation.locationOf(first));
    assertNotEquals(sourceLocation.getRegister().getName(), firstLocation.getRegister().getName());
  }

  private static void addArg(MachineBasicBlock block, VirtualRegister destination) {
    MachineInstr instruction = new MachineInstr(MachineOpcode.ARG_IN, destination);
    instruction.setType(destination.getType());
    block.addInstruction(instruction);
  }

  private static void add(
      MachineBasicBlock block, VirtualRegister destination, VirtualRegister left,
      accela.backend.machine.MachineOperand right) {
    MachineInstr instruction = new MachineInstr(
        destination.getType().isFloat() ? MachineOpcode.FADD : MachineOpcode.ADD, destination);
    instruction.addOperand(new VRegOperand(left));
    instruction.addOperand(right);
    instruction.setType(destination.getType());
    block.addInstruction(instruction);
  }

  private static void addReturn(MachineBasicBlock block, VirtualRegister value) {
    MachineInstr instruction = new MachineInstr(MachineOpcode.RET, null);
    instruction.addOperand(new VRegOperand(value));
    instruction.setType(value.getType());
    block.addInstruction(instruction);
  }

  private static void addCall(MachineBasicBlock block) {
    MachineInstr instruction = new MachineInstr(MachineOpcode.CALL, null);
    instruction.setCallee("callee");
    block.addInstruction(instruction);
  }

}
