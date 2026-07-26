package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

public final class IteratedRegisterAllocator implements RegisterAllocator {
  private static final int MAX_REWRITE_ROUNDS = 20;

  private final TargetRegisterInfo registers = new TargetRegisterInfo();
  private final LocalSpillRewriter spillRewriter = new LocalSpillRewriter();

  @Override
  public AllocationResult allocate(MachineFunction function, RISCVTarget target) {
    Map<VirtualRegister, StackSlot> spilledLocations = new LinkedHashMap<>();
    for (int round = 0; round < MAX_REWRITE_ROUNDS; round++) {
      LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
      InterferenceGraphBuilder.Result built = InterferenceGraphBuilder.build(function, liveness);
      AllocatorState state =
          new AllocatorState(
              built,
              registers,
              SpillCostAnalysis.analyze(function, built.graph()),
              liveAcrossCall(function, liveness, target),
              argumentRegisterHazards(function));
      state.makeWorklist();

      while (hasWork(state)) {
        if (!state.simplifyWorklist.isEmpty()) {
          state.simplify();
        } else if (!state.worklistMoves.isEmpty()) {
          state.coalesce();
        } else if (!state.freezeWorklist.isEmpty()) {
          state.freeze();
        } else if (!state.spillWorklist.isEmpty()) {
          state.selectSpill();
        }
      }

      state.assignColors();

      if (state.spilledNodes.isEmpty()) {
        return toAllocationResult(state, registers, spilledLocations);
      }

      spilledLocations.putAll(
          spillRewriter.rewrite(function, state.spilledNodes, target));
    }

    throw new IllegalStateException("register allocation did not converge after spill rewriting");
  }

  private static boolean hasWork(AllocatorState state) {
    return !state.simplifyWorklist.isEmpty()
        || !state.worklistMoves.isEmpty()
        || !state.freezeWorklist.isEmpty()
        || !state.spillWorklist.isEmpty();
  }

  private static AllocationResult toAllocationResult(
      AllocatorState state,
      TargetRegisterInfo registers,
      Map<VirtualRegister, StackSlot> spilledLocations) {
    AllocationResult result = new AllocationResult();
    for (Map.Entry<VirtualRegister, StackSlot> entry : spilledLocations.entrySet()) {
      result.put(entry.getKey(), new StackLocation(entry.getValue()));
    }
    for (Map.Entry<VirtualRegister, PhysicalRegister> entry : state.color.entrySet()) {
      result.put(entry.getKey(), new RegisterLocation(entry.getValue()));
      if (registers.isCalleeSaved(entry.getValue())) {
        result.addUsedCalleeSavedRegister(entry.getValue());
      }
    }
    return result;
  }

  private static Set<VirtualRegister> liveAcrossCall(
      MachineFunction function, LivenessAnalysis.Result liveness, RISCVTarget target) {
    Set<VirtualRegister> result = new HashSet<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instr : block.getInstructions()) {
        if (clobbersCallerSaved(instr, target)) {
          for (VirtualRegister register : liveness.liveAfter(instr)) {
            if (liveness.liveBefore(instr).contains(register)) {
              result.add(register);
            }
          }
        }
      }
    }
    return result;
  }

  private static boolean clobbersCallerSaved(MachineInstr instr, RISCVTarget target) {
    if (instr.getOpcode() == MachineOpcode.CALL) return true;
    return instr.getOpcode() == MachineOpcode.MEMZERO
        && target.shouldUseMemzeroHelper(
            (int) ((ImmOperand) instr.getOperands().get(1)).getValue());
  }

  private static Set<VirtualRegister> argumentRegisterHazards(MachineFunction function) {
    Set<VirtualRegister> result = new HashSet<>(function.getArguments());
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getOpcode() != MachineOpcode.CALL) continue;
        for (var operand : instr.getOperands()) {
          if (operand instanceof VRegOperand register) result.add(register.getRegister());
        }
      }
    }
    return result;
  }
}
