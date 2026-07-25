package accela.backend.regalloc;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

public final class IteratedRegisterAllocator implements RegisterAllocator {
  private static final int MAX_REWRITE_ROUNDS = 20;

  private final TargetRegisterInfo registers = new TargetRegisterInfo();
  private final LocalSpillRewriter spillRewriter = new LocalSpillRewriter();

  @Override
  public AllocationResult allocate(MachineFunction function, RISCVTarget target) {
    for (int round = 0; round < MAX_REWRITE_ROUNDS; round++) {
      LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
      InterferenceGraphBuilder.Result built = InterferenceGraphBuilder.build(function, liveness);
      AllocatorState state =
          new AllocatorState(built, registers, ignored -> 1.0, liveAcrossCall(function, liveness));
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
        return toAllocationResult(state, registers);
      }

      spillRewriter.rewrite(function, state.spilledNodes, target);
    }

    throw new IllegalStateException("register allocation did not converge after spill rewriting");
  }

  private static boolean hasWork(AllocatorState state) {
    return !state.simplifyWorklist.isEmpty()
        || !state.worklistMoves.isEmpty()
        || !state.freezeWorklist.isEmpty()
        || !state.spillWorklist.isEmpty();
  }

  private static AllocationResult toAllocationResult(AllocatorState state, TargetRegisterInfo registers) {
    AllocationResult result = new AllocationResult();
    for (Map.Entry<VirtualRegister, PhysicalRegister> entry : state.color.entrySet()) {
      result.put(entry.getKey(), new RegisterLocation(entry.getValue()));
      if (registers.isCalleeSaved(entry.getValue())) {
        result.addUsedCalleeSavedRegister(entry.getValue());
      }
    }
    return result;
  }

  private static Set<VirtualRegister> liveAcrossCall(
      MachineFunction function, LivenessAnalysis.Result liveness) {
    Set<VirtualRegister> result = new HashSet<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getOpcode() == MachineOpcode.CALL) {
          result.addAll(liveness.liveAfter(instr));
        }
      }
    }
    return result;
  }
}
