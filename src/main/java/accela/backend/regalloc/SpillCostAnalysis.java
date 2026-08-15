package accela.backend.regalloc;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import accela.cost.HotnessModel;

final class SpillCostAnalysis {
  private SpillCostAnalysis() {}

  static SpillCostModel analyze(MachineFunction function, InterferenceGraph graph) {
    Map<MachineBasicBlock, List<MachineBasicBlock>> successors =
        computeSuccessors(function);
    MachineBasicBlock entry = function.getEntryBlock();
    Set<MachineBasicBlock> reachable =
        entry == null ? Set.of() : computeReachable(entry, successors);
    Map<MachineBasicBlock, Integer> loopDepths =
        computeLoopDepths(function, successors, reachable);
    Map<VirtualRegister, Double> weightedReferences = new HashMap<>();
    Map<VirtualRegister, Integer> analyzedDegrees = new HashMap<>();
    for (VirtualRegister register : graph.nodes()) {
      analyzedDegrees.put(register, graph.degree(register));
    }

    for (MachineBasicBlock block : function.getBlocks()) {
      if (!reachable.contains(block)) {
        continue;
      }
      double referenceWeight =
          loopReferenceWeight(loopDepths.getOrDefault(block, 0));
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getDest() != null) {
          weightedReferences.merge(
              instr.getDest(), referenceWeight, SpillCostAnalysis::saturatingAdd);
        }
        for (var operand : instr.getOperands()) {
          if (operand instanceof VRegOperand) {
            weightedReferences.merge(
                ((VRegOperand) operand).getRegister(),
                referenceWeight,
                SpillCostAnalysis::saturatingAdd);
          }
        }
      }
    }

    return new SpillCostModel() {
      @Override
      public double cost(VirtualRegister register) {
        return normalizedCost(
            weightedReferences, register, analyzedDegrees.getOrDefault(register, 0));
      }

      @Override
      public double cost(VirtualRegister register, int degree) {
        return normalizedCost(weightedReferences, register, degree);
      }

      @Override
      public void combine(VirtualRegister representative, VirtualRegister merged) {
        weightedReferences.merge(
            representative,
            weightedReferences.getOrDefault(merged, 0.0),
            SpillCostAnalysis::saturatingAdd);
        weightedReferences.remove(merged);
      }
    };
  }

  private static double normalizedCost(
      Map<VirtualRegister, Double> weightedReferences,
      VirtualRegister register,
      int degree) {
    return weightedReferences.getOrDefault(register, 0.0) / Math.max(1, degree);
  }

  private static double loopReferenceWeight(int loopDepth) {
    return HotnessModel.DEFAULT.loopReferenceWeight(loopDepth);
  }

  private static double saturatingAdd(double left, double right) {
    double sum = left + right;
    return Double.isFinite(sum) ? sum : Double.MAX_VALUE;
  }

  private static Map<MachineBasicBlock, Integer> computeLoopDepths(
      MachineFunction function,
      Map<MachineBasicBlock, List<MachineBasicBlock>> successors,
      Set<MachineBasicBlock> reachable) {
    Map<MachineBasicBlock, Integer> depths = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      depths.put(block, 0);
    }
    MachineBasicBlock entry = function.getEntryBlock();
    if (entry == null) {
      return depths;
    }

    Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors =
        computePredecessors(function, successors);
    Map<MachineBasicBlock, Set<MachineBasicBlock>> dominators =
        computeDominators(function, entry, predecessors, reachable);

    Map<MachineBasicBlock, Set<MachineBasicBlock>> latchesByHeader =
        new IdentityHashMap<>();
    for (MachineBasicBlock tail : reachable) {
      for (MachineBasicBlock header : successors.getOrDefault(tail, List.of())) {
        if (dominators.get(tail).contains(header)) {
          latchesByHeader
              .computeIfAbsent(header, ignored -> new HashSet<>())
              .add(tail);
        }
      }
    }

    // Multiple latches with the same header belong to one natural loop and
    // therefore contribute one level of nesting, not one level per back edge.
    for (Map.Entry<MachineBasicBlock, Set<MachineBasicBlock>> loop :
        latchesByHeader.entrySet()) {
      Set<MachineBasicBlock> loopBlocks =
          collectNaturalLoop(loop.getKey(), loop.getValue(), predecessors, reachable);
      for (MachineBasicBlock block : loopBlocks) {
        depths.put(block, depths.get(block) + 1);
      }
    }
    return depths;
  }

  private static Map<MachineBasicBlock, List<MachineBasicBlock>> computeSuccessors(
      MachineFunction function) {
    Set<MachineBasicBlock> functionBlocks = new HashSet<>(function.getBlocks());
    Map<MachineBasicBlock, List<MachineBasicBlock>> successors =
        new IdentityHashMap<>();

    for (MachineBasicBlock block : function.getBlocks()) {
      LinkedHashSet<MachineBasicBlock> targets = new LinkedHashSet<>();
      if (!block.getInstructions().isEmpty()) {
        MachineInstr terminator = block.getInstructions().getLast();
        if (terminator.getOpcode() == MachineOpcode.BR
            || terminator.getOpcode() == MachineOpcode.CONDBR) {
          for (var operand : terminator.getOperands()) {
            if (operand instanceof BlockOperand) {
              MachineBasicBlock target = ((BlockOperand) operand).getBlock();
              if (functionBlocks.contains(target)) {
                targets.add(target);
              }
            }
          }
        }
      }
      successors.put(block, new ArrayList<>(targets));
    }
    return successors;
  }

  private static Map<MachineBasicBlock, List<MachineBasicBlock>> computePredecessors(
      MachineFunction function,
      Map<MachineBasicBlock, List<MachineBasicBlock>> successors) {
    Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors =
        new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      predecessors.put(block, new ArrayList<>());
    }
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineBasicBlock successor : successors.get(block)) {
        predecessors.get(successor).add(block);
      }
    }
    return predecessors;
  }

  private static Set<MachineBasicBlock> computeReachable(
      MachineBasicBlock entry,
      Map<MachineBasicBlock, List<MachineBasicBlock>> successors) {
    Set<MachineBasicBlock> reachable = new HashSet<>();
    Deque<MachineBasicBlock> worklist = new ArrayDeque<>();
    reachable.add(entry);
    worklist.push(entry);

    while (!worklist.isEmpty()) {
      MachineBasicBlock block = worklist.pop();
      for (MachineBasicBlock successor : successors.getOrDefault(block, List.of())) {
        if (reachable.add(successor)) {
          worklist.push(successor);
        }
      }
    }
    return reachable;
  }

  private static Map<MachineBasicBlock, Set<MachineBasicBlock>> computeDominators(
      MachineFunction function,
      MachineBasicBlock entry,
      Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors,
      Set<MachineBasicBlock> reachable) {
    Map<MachineBasicBlock, Set<MachineBasicBlock>> dominators =
        new IdentityHashMap<>();
    for (MachineBasicBlock block : reachable) {
      dominators.put(
          block,
          block == entry ? new HashSet<>(Set.of(entry)) : new HashSet<>(reachable));
    }

    boolean changed;
    do {
      changed = false;
      for (MachineBasicBlock block : function.getBlocks()) {
        if (block == entry || !reachable.contains(block)) {
          continue;
        }

        Set<MachineBasicBlock> newDominators = null;
        for (MachineBasicBlock predecessor : predecessors.get(block)) {
          if (!reachable.contains(predecessor)) {
            continue;
          }
          if (newDominators == null) {
            newDominators = new HashSet<>(dominators.get(predecessor));
          } else {
            newDominators.retainAll(dominators.get(predecessor));
          }
        }
        if (newDominators == null) {
          newDominators = new HashSet<>();
        }
        newDominators.add(block);
        if (!newDominators.equals(dominators.get(block))) {
          dominators.put(block, newDominators);
          changed = true;
        }
      }
    } while (changed);

    return dominators;
  }

  private static Set<MachineBasicBlock> collectNaturalLoop(
      MachineBasicBlock header,
      Set<MachineBasicBlock> latches,
      Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors,
      Set<MachineBasicBlock> reachable) {
    Set<MachineBasicBlock> loopBlocks = new HashSet<>();
    Deque<MachineBasicBlock> worklist = new ArrayDeque<>();
    loopBlocks.add(header);
    for (MachineBasicBlock latch : latches) {
      if (loopBlocks.add(latch)) {
        worklist.push(latch);
      }
    }

    while (!worklist.isEmpty()) {
      MachineBasicBlock block = worklist.pop();
      for (MachineBasicBlock predecessor : predecessors.get(block)) {
        if (reachable.contains(predecessor) && loopBlocks.add(predecessor)) {
          worklist.push(predecessor);
        }
      }
    }
    return loopBlocks;
  }
}
