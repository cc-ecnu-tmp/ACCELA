package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.StackSlotOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class LocalSpillRewriter {
  boolean rewrite(
      MachineFunction function,
      Set<VirtualRegister> spilledRegisters,
      RISCVTarget target) {
    if (spilledRegisters.isEmpty()) {
      return false;
    }

    Map<VirtualRegister, StackSlot> slots = new LinkedHashMap<>();
    for (VirtualRegister register : spilledRegisters) {
      slots.put(register, createSpillSlot(function, register, target));
    }

    for (MachineBasicBlock block : function.getBlocks()) {
      rewriteBlock(function, block, slots);
    }

    return true;
  }

  private void rewriteBlock(
      MachineFunction function,
      MachineBasicBlock block,
      Map<VirtualRegister, StackSlot> slots) {
    List<MachineInstr> rewritten = new ArrayList<>();

    for (MachineInstr instr : block.getInstructions()) {
      Map<VirtualRegister, VirtualRegister> reloads = new HashMap<>();
      List<MachineOperand> operands = new ArrayList<>();

      for (MachineOperand operand : instr.getOperands()) {
        if (operand instanceof VRegOperand) {
          VirtualRegister register = ((VRegOperand) operand).getRegister();
          StackSlot slot = slots.get(register);
          if (slot != null) {
            VirtualRegister reload =
                reloads.computeIfAbsent(
                    register, ignored -> insertReload(function, rewritten, register, slot));
            operands.add(new VRegOperand(reload));
            continue;
          }
        }
        operands.add(operand);
      }

      VirtualRegister originalDest = instr.getDest();
      StackSlot destSlot = originalDest == null ? null : slots.get(originalDest);
      VirtualRegister rewrittenDest = originalDest;
      if (destSlot != null) {
        rewrittenDest = function.createVirtualRegister(originalDest.getType(), originalDest.getHint() + ".spill.def");
      }

      MachineInstr rewrittenInstr = cloneInstr(instr, rewrittenDest, operands);
      rewritten.add(rewrittenInstr);

      if (destSlot != null) {
        insertStore(function, rewritten, rewrittenDest, destSlot);
      }
    }

    block.getInstructions().clear();
    block.getInstructions().addAll(rewritten);
  }

  private VirtualRegister insertReload(
      MachineFunction function,
      List<MachineInstr> rewritten,
      VirtualRegister spilled,
      StackSlot slot) {
    VirtualRegister address = function.createVirtualRegister(MachineType.PTR, spilled.getHint() + ".spill.addr");
    MachineInstr stackAddr = new MachineInstr(MachineOpcode.STACK_ADDR, address);
    stackAddr.addOperand(new StackSlotOperand(slot));
    stackAddr.setType(MachineType.PTR);
    rewritten.add(stackAddr);

    VirtualRegister reload = function.createVirtualRegister(spilled.getType(), spilled.getHint() + ".spill.reload");
    MachineInstr load = new MachineInstr(MachineOpcode.LOAD, reload);
    load.addOperand(new VRegOperand(address));
    load.setType(spilled.getType());
    rewritten.add(load);
    return reload;
  }

  private void insertStore(
      MachineFunction function,
      List<MachineInstr> rewritten,
      VirtualRegister value,
      StackSlot slot) {
    VirtualRegister address = function.createVirtualRegister(MachineType.PTR, value.getHint() + ".spill.addr");
    MachineInstr stackAddr = new MachineInstr(MachineOpcode.STACK_ADDR, address);
    stackAddr.addOperand(new StackSlotOperand(slot));
    stackAddr.setType(MachineType.PTR);
    rewritten.add(stackAddr);

    MachineInstr store = new MachineInstr(MachineOpcode.STORE, null);
    store.addOperand(new VRegOperand(value));
    store.addOperand(new VRegOperand(address));
    store.setType(value.getType());
    rewritten.add(store);
  }

  private MachineInstr cloneInstr(
      MachineInstr instr,
      VirtualRegister dest,
      List<MachineOperand> operands) {
    MachineInstr clone = new MachineInstr(instr.getOpcode(), dest);
    for (MachineOperand operand : operands) {
      clone.addOperand(operand);
    }
    clone.setType(instr.getType());
    clone.setPredicate(instr.getPredicate());
    clone.setCallee(instr.getCallee());
    return clone;
  }

  private StackSlot createSpillSlot(
      MachineFunction function,
      VirtualRegister register,
      RISCVTarget target) {
    return function
        .getFrameInfo()
        .createSpillSlot(
            register.getType(),
            target.stackSizeOf(register.getType()),
            target.stackAlignOf(register.getType()));
  }
}
