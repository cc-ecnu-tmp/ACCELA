package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class AllSpillRegisterAllocator implements RegisterAllocator {
  @Override
  public AllocationResult allocate(MachineFunction function, RISCVTarget target) {
    AllocationResult result = new AllocationResult();
    Set<VirtualRegister> registers = new LinkedHashSet<>();
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    InterferenceGraph interference = InterferenceGraph.build(function, liveness);
    collectRegisters(function, registers);
    Set<VirtualRegister> callConflicts =
        CallClobberAnalysis.analyze(function, liveness, target);
    Set<VirtualRegister> argumentHazards = collectArgumentHazards(function);
    Map<VirtualRegister, Integer> spillWeights = collectSpillWeights(function);
    colorRegisters(function, registers, callConflicts, argumentHazards,
        spillWeights, interference, target, result, false);
    colorRegisters(function, registers, callConflicts, argumentHazards,
        spillWeights, interference, target, result, true);

    Map<VirtualRegister, StackSlot> spills = new LinkedHashMap<>();
    Map<StackSlot, List<VirtualRegister>> occupants = new LinkedHashMap<>();
    for (VirtualRegister register : registers) {
      if (result.locationOf(register) != null) continue;
      StackSlot slot = null;
      for (var entry : occupants.entrySet()) {
        List<VirtualRegister> assigned = entry.getValue();
        if (assigned.get(0).getType() == register.getType()
            && assigned.stream().noneMatch(other -> interference.interferes(register, other))) {
          slot = entry.getKey();
          break;
        }
      }
      if (slot == null) {
        slot = function.getFrameInfo().createSpillSlot(
            register.getType(), target.stackSizeOf(register.getType()),
            target.stackAlignOf(register.getType()));
        occupants.put(slot, new ArrayList<>());
      }
      occupants.get(slot).add(register);
      spills.put(register, slot);
    }
    for (Map.Entry<VirtualRegister, StackSlot> entry : spills.entrySet()) {
      result.put(entry.getKey(), new StackLocation(entry.getValue()));
    }
    return result;
  }

  private static void colorRegisters(
      MachineFunction function,
      Set<VirtualRegister> registers,
      Set<VirtualRegister> callConflicts,
      Set<VirtualRegister> argumentHazards,
      Map<VirtualRegister, Integer> spillWeights,
      InterferenceGraph interference,
      RISCVTarget target,
      AllocationResult result,
      boolean floatingPoint) {
    List<VirtualRegister> candidates = registers.stream()
        .filter(register -> register.getType().isFloat() == floatingPoint)
        .toList();
    var colors = target.getAllocatableRegisters(floatingPoint ? MachineType.F32 : MachineType.I32);
    var assignments = OptimisticGraphColoring.color(
        candidates, interference, colors.size(),
        register -> spillWeights.getOrDefault(register, 1),
        (register, color) ->
            (!callConflicts.contains(register) || target.isCalleeSaved(colors.get(color)))
                && (!argumentHazards.contains(register)
                    || !target.isArgumentRegister(colors.get(color))));
    for (var entry : assignments.entrySet()) {
      if (entry.getValue() >= 0) {
        var physicalRegister = colors.get(entry.getValue());
        result.put(entry.getKey(), new RegisterLocation(physicalRegister));
        if (target.isCalleeSaved(physicalRegister)) {
          if (physicalRegister.getType().isFloat()) {
            function.getFrameInfo().markFloatCalleeSavedRegister(physicalRegister.getName());
          } else {
            function.getFrameInfo().markCalleeSavedRegister(physicalRegister.getName());
          }
        }
      }
    }
  }

  private static Map<VirtualRegister, Integer> collectSpillWeights(MachineFunction function) {
    Map<VirtualRegister, Integer> weights = new IdentityHashMap<>();
    Set<MachineBasicBlock> loopBlocks = MachineLoopAnalysis.findLoopBlocks(function);
    for (MachineBasicBlock block : function.getBlocks()) {
      int weight = loopBlocks.contains(block) ? 16 : 1;
      for (MachineInstr instruction : block.getInstructions()) {
        if (instruction.getDest() != null) {
          weights.merge(instruction.getDest(), weight, Integer::sum);
        }
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) {
            weights.merge(register.getRegister(), weight, Integer::sum);
          }
        }
      }
    }
    return weights;
  }

  private static Set<VirtualRegister> collectArgumentHazards(MachineFunction function) {
    Set<VirtualRegister> hazards = new LinkedHashSet<>(function.getArguments());
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        if (instruction.getOpcode() != accela.backend.machine.MachineOpcode.CALL) continue;
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) hazards.add(register.getRegister());
        }
      }
    }
    return hazards;
  }

  private static void collectRegisters(
      MachineFunction function, Set<VirtualRegister> registers) {
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        if (instruction.getDest() != null) registers.add(instruction.getDest());
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register) registers.add(register.getRegister());
        }
      }
    }
  }
}
