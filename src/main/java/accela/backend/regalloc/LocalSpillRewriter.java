package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.FloatImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegOperand;
import accela.backend.machine.StackSlotOperand;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.machine.RVVConfig;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class LocalSpillRewriter {
  Map<VirtualRegister, StackSlot> rewrite(
      MachineFunction function,
      Set<VirtualRegister> spilledRegisters,
      RISCVTarget target) {
    if (spilledRegisters.isEmpty()) {
      return Map.of();
    }

    Set<VirtualRegister> effectiveSpills =
        expandOverfullCallArgumentSpills(function, spilledRegisters, target);
    Map<VirtualRegister, MachineInstr> rematerializable =
        findRematerializableGlobalAddresses(function, effectiveSpills);
    Map<VirtualRegister, StackSlot> slots = new LinkedHashMap<>();
    for (VirtualRegister register : effectiveSpills) {
      if (rematerializable.containsKey(register)) continue;
      slots.put(register, createSpillSlot(function, register, target));
    }

    for (MachineBasicBlock block : function.getBlocks()) {
      rewriteBlock(function, block, slots, rematerializable, target);
    }

    return slots;
  }

  private Set<VirtualRegister> expandOverfullCallArgumentSpills(
      MachineFunction function,
      Set<VirtualRegister> selectedSpills,
      RISCVTarget target) {
    Set<VirtualRegister> result = new HashSet<>(selectedSpills);
    TargetRegisterInfo registerInfo = new TargetRegisterInfo();

    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getOpcode() != MachineOpcode.CALL) continue;

        Set<VirtualRegister> integerArguments = new HashSet<>();
        Set<VirtualRegister> floatArguments = new HashSet<>();
        Set<VirtualRegister> stackIntegerArguments = new HashSet<>();
        Set<VirtualRegister> stackFloatArguments = new HashSet<>();
        RISCVTarget.CallArgCursor cursor = target.newCallArgCursor();

        for (int i = 0; i < instr.getOperands().size(); i++) {
          MachineOperand operand = instr.getOperands().get(i);
          MachineType type = callOperandType(instr, i, operand);
          RISCVTarget.CallArgAssignment assignment =
              target.assignCallArg(cursor, type);
          if (!(operand instanceof VRegOperand)) continue;

          VirtualRegister register = ((VRegOperand) operand).getRegister();
          Set<VirtualRegister> arguments =
              type.isFloat() ? floatArguments : integerArguments;
          arguments.add(register);
          if (!assignment.isInRegister()) {
            (type.isFloat() ? stackFloatArguments : stackIntegerArguments)
                .add(register);
          }
        }

        if (integerArguments.size()
            > registerInfo.nonArgumentRegisters(MachineType.I32).size()) {
          result.addAll(stackIntegerArguments);
        }
        if (floatArguments.size()
            > registerInfo.nonArgumentRegisters(MachineType.F32).size()) {
          result.addAll(stackFloatArguments);
        }
      }
    }

    return result;
  }

  private void rewriteBlock(
      MachineFunction function,
      MachineBasicBlock block,
      Map<VirtualRegister, StackSlot> slots,
      Map<VirtualRegister, MachineInstr> rematerializable,
      RISCVTarget target) {
    List<MachineInstr> rewritten = new ArrayList<>();
    Map<VirtualRegister, VirtualRegister> blockRematerializations = new HashMap<>();

    for (MachineInstr instr : block.getInstructions()) {
      if (instr.getDest() != null && rematerializable.get(instr.getDest()) == instr) continue;
      Map<VirtualRegister, VirtualRegister> reloads = new HashMap<>();
      List<MachineOperand> operands = new ArrayList<>();
      RISCVTarget.CallArgCursor callArgs =
          instr.getOpcode() == MachineOpcode.CALL ? target.newCallArgCursor() : null;

      for (int i = 0; i < instr.getOperands().size(); i++) {
        MachineOperand operand = instr.getOperands().get(i);
        RISCVTarget.CallArgAssignment callAssignment = null;
        if (callArgs != null) {
          callAssignment =
              target.assignCallArg(callArgs, callOperandType(instr, i, operand));
        }
        if (operand instanceof VRegOperand) {
          VirtualRegister register = ((VRegOperand) operand).getRegister();
          MachineInstr recipe = rematerializable.get(register);
          if (recipe != null) {
      VirtualRegister materialized = blockRematerializations.computeIfAbsent(
                register, ignored -> rematerialize(function, rewritten, register, recipe));
            operands.add(new VRegOperand(materialized));
            continue;
          }
          StackSlot slot = slots.get(register);
          if (slot != null) {
            if (callAssignment != null && !callAssignment.isInRegister()) {
              operands.add(new StackSlotOperand(slot));
              continue;
            }
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
        rewrittenDest =
            originalDest.getType().isVector()
                ? function.createVectorRegister(
                    originalDest.getVectorShape(), originalDest.getHint() + ".spill.def")
                : function.createVirtualRegister(
                    originalDest.getType(), originalDest.getHint() + ".spill.def");
      }

      MachineInstr rewrittenInstr = cloneInstr(instr, rewrittenDest, operands);
      rewritten.add(rewrittenInstr);

      if (destSlot != null) {
        insertStore(function, rewritten, rewrittenDest, destSlot);
      }
      if (instr.getOpcode() == MachineOpcode.CALL) blockRematerializations.clear();
    }

    block.getInstructions().clear();
    block.getInstructions().addAll(rewritten);
  }

  private static Map<VirtualRegister, MachineInstr> findRematerializableGlobalAddresses(
      MachineFunction function, Set<VirtualRegister> spilledRegisters) {
    Map<VirtualRegister, MachineInstr> definitions = new HashMap<>();
    Set<VirtualRegister> multipleDefinitions = new HashSet<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        VirtualRegister destination = instruction.getDest();
        if (destination == null || !spilledRegisters.contains(destination)) continue;
        if (definitions.putIfAbsent(destination, instruction) != null) {
          multipleDefinitions.add(destination);
        }
      }
    }
    definitions.entrySet().removeIf(entry ->
        multipleDefinitions.contains(entry.getKey())
            || entry.getValue().getOpcode() != MachineOpcode.MOVE
            || entry.getValue().getOperands().size() != 1
            || !(entry.getValue().getOperands().getFirst() instanceof SymbolOperand));
    return definitions;
  }

  private static VirtualRegister rematerialize(
      MachineFunction function,
      List<MachineInstr> rewritten,
      VirtualRegister original,
      MachineInstr recipe) {
    VirtualRegister result = function.createVirtualRegister(
        original.getType(), original.getHint() + ".remat");
    MachineInstr materialize = new MachineInstr(recipe.getOpcode(), result);
    materialize.setType(recipe.getType());
    for (int index = 0; index < recipe.getOperands().size(); index++) {
      materialize.addOperand(recipe.getOperands().get(index), recipe.getOperandType(index));
    }
    rewritten.add(materialize);
    return result;
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

    VirtualRegister reload =
        spilled.getType().isVector()
            ? function.createVectorRegister(
                spilled.getVectorShape(), spilled.getHint() + ".spill.reload")
            : function.createVirtualRegister(
                spilled.getType(), spilled.getHint() + ".spill.reload");
    MachineInstr load =
        new MachineInstr(
            spilled.getType().isVector() ? MachineOpcode.VLOAD : MachineOpcode.LOAD, reload);
    load.addOperand(new VRegOperand(address));
    load.setType(spilled.getType());
    if (spilled.getType().isVector()) {
      load.setRVVConfig(RVVConfig.forShape(spilled.getVectorShape()));
    }
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

    MachineInstr store =
        new MachineInstr(
            value.getType().isVector() ? MachineOpcode.VSTORE : MachineOpcode.STORE, null);
    store.addOperand(new VRegOperand(value));
    store.addOperand(new VRegOperand(address));
    store.setType(value.getType());
    if (value.getType().isVector()) {
      store.setRVVConfig(RVVConfig.forShape(value.getVectorShape()));
    }
    rewritten.add(store);
  }

  private MachineInstr cloneInstr(
      MachineInstr instr,
      VirtualRegister dest,
      List<MachineOperand> operands) {
    MachineInstr clone = new MachineInstr(instr.getOpcode(), dest);
    for (int i = 0; i < operands.size(); i++) {
      clone.addOperand(operands.get(i), instr.getOperandType(i));
    }
    clone.setType(instr.getType());
    clone.setPredicate(instr.getPredicate());
    clone.setCallee(instr.getCallee());
    clone.setRVVConfig(instr.getRVVConfig());
    clone.setVCIXInfo(instr.getVCIXInfo());
    return clone;
  }

  private MachineType callOperandType(
      MachineInstr call, int operandIndex, MachineOperand operand) {
    MachineType recorded = call.getOperandType(operandIndex);
    if (recorded != null) return recorded;
    if (operand instanceof VRegOperand) {
      return ((VRegOperand) operand).getRegister().getType();
    }
    if (operand instanceof FloatImmOperand) return MachineType.F32;
    if (operand instanceof SymbolOperand) return MachineType.PTR;
    if (operand instanceof StackSlotOperand) {
      return ((StackSlotOperand) operand).getSlot().getType();
    }
    if (operand instanceof PhysicalRegOperand) {
      return ((PhysicalRegOperand) operand).getRegister().getType();
    }
    return MachineType.I32;
  }

  private StackSlot createSpillSlot(
      MachineFunction function,
      VirtualRegister register,
      RISCVTarget target) {
    if (register.getType().isVector()) {
      int size = register.getVectorShape().semanticSizeBytes();
      int align = Math.min(16, Integer.highestOneBit(Math.max(1, size)));
      return function
          .getFrameInfo()
          .createSpillSlot(MachineType.VECTOR, size, align);
    }
    return function
        .getFrameInfo()
        .createSpillSlot(
            register.getType(),
            target.stackSizeOf(register.getType()),
            target.stackAlignOf(register.getType()));
  }
}
