package accela.backend.regalloc;

import accela.backend.frame.StackSlot;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.PhysicalRegister;
import accela.backend.machine.PhysicalRegOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.HashMap;
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
    LiveRangeSplitting.run(function, target);
    Map<VirtualRegister, StackSlot> spilledLocations = new LinkedHashMap<>();
    for (int round = 0; round < MAX_REWRITE_ROUNDS; round++) {
      LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
      InterferenceGraphBuilder.Result built = InterferenceGraphBuilder.build(function, liveness);
      FixedRegisterConstraints fixed = fixedRegisterConstraints(function, target);
      AllocatorState state =
          new AllocatorState(
              built,
              registers,
              SpillCostAnalysis.analyze(function, built.graph()),
              liveAcrossCall(function, liveness, target),
              fixed.hazards(),
              fixed.affinities());
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

  @Override
  public AllocationEstimate estimate(MachineFunction function, RISCVTarget target) {
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    InterferenceGraphBuilder.Result built = InterferenceGraphBuilder.build(function, liveness);
    FixedRegisterConstraints fixed = fixedRegisterConstraints(function, target);
    SpillCostModel spillCosts = SpillCostAnalysis.analyze(function, built.graph());
    AllocatorState state =
        new AllocatorState(
            built,
            registers,
            spillCosts,
            liveAcrossCall(function, liveness, target),
            fixed.hazards(),
            fixed.affinities());
    state.makeWorklist();
    while (hasWork(state)) {
      if (!state.simplifyWorklist.isEmpty()) state.simplify();
      else if (!state.worklistMoves.isEmpty()) state.coalesce();
      else if (!state.freezeWorklist.isEmpty()) state.freeze();
      else state.selectSpill();
    }
    state.assignColors();

    double spillWeight = 0.0;
    for (VirtualRegister register : state.spilledNodes) {
      spillWeight += spillCosts.cost(register, built.graph().degree(register));
    }
    int maxIntegerLive = 0;
    int maxFloatLive = 0;
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        int integers = 0;
        int floats = 0;
        Set<VirtualRegister> live = new HashSet<>(liveness.liveBefore(instruction));
        live.addAll(liveness.liveAfter(instruction));
        for (VirtualRegister register : live) {
          if (register.getType().isFloat()) floats++;
          else integers++;
        }
        maxIntegerLive = Math.max(maxIntegerLive, integers);
        maxFloatLive = Math.max(maxFloatLive, floats);
      }
    }
    return new AllocationEstimate(
        state.spilledNodes.size(),
        spillWeight,
        maxIntegerLive,
        maxFloatLive,
        (int) state.color.values().stream().filter(registers::isCalleeSaved).distinct().count(),
        state.constrainedMoves.size() + state.frozenMoves.size());
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

  private FixedRegisterConstraints fixedRegisterConstraints(
      MachineFunction function, RISCVTarget target) {
    Set<VirtualRegister> hazards = new HashSet<>();
    Map<VirtualRegister, String> affinities = new HashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getOpcode() == MachineOpcode.ARG_IN) {
          PhysicalRegister source =
              instr.getOperands().getFirst() instanceof PhysicalRegOperand physical
                  ? physical.getRegister()
                  : null;
          addAffinity(instr.getDest(), source, hazards, affinities);
        } else if (instr.getOpcode() == MachineOpcode.CALL) {
          RISCVTarget.CallArgCursor cursor = target.newCallArgCursor();
          for (int i = 0; i < instr.getOperands().size(); i++) {
            MachineOperand operand = instr.getOperands().get(i);
            MachineType type = instr.getOperandType(i);
            if (type == null && operand instanceof VRegOperand register) {
              type = register.getRegister().getType();
            }
            RISCVTarget.CallArgAssignment assignment = target.assignCallArg(cursor, type);
            if (operand instanceof VRegOperand register && assignment.isInRegister()) {
              addAffinity(
                  register.getRegister(),
                  assignment.getRegister(),
                  hazards,
                  affinities);
            }
          }
          if (instr.getDest() != null) {
            addAffinity(
                instr.getDest(),
                target.getReturnRegister(instr.getType()),
                hazards,
                affinities);
          }
        } else if (instr.getOpcode() == MachineOpcode.RET
            && !instr.getOperands().isEmpty()
            && instr.getOperands().getFirst() instanceof VRegOperand value) {
          addAffinity(
              value.getRegister(),
              target.getReturnRegister(instr.getType()),
              hazards,
              affinities);
        }
      }
    }
    return new FixedRegisterConstraints(hazards, affinities);
  }

  private void addAffinity(
      VirtualRegister register,
      PhysicalRegister fixed,
      Set<VirtualRegister> hazards,
      Map<VirtualRegister, String> affinities) {
    if (hazards.contains(register)) return;
    if (fixed == null
        || fixed.getType().isFloat() != register.getType().isFloat()
        || registers.isAllocatorReserved(fixed)) {
      hazards.add(register);
      affinities.remove(register);
      return;
    }
    // Restrict an SSA value to its exact ABI register or a non-argument register.
    // This removes copies without introducing argument-register copy cycles.
    String previous = affinities.putIfAbsent(register, fixed.getName());
    if (previous != null && !previous.equals(fixed.getName())) {
      hazards.add(register);
      affinities.remove(register);
    }
  }

  private record FixedRegisterConstraints(
      Set<VirtualRegister> hazards, Map<VirtualRegister, String> affinities) {}
}
