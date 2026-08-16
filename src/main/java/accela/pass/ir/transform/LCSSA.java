package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Forms loop-closed SSA for values defined in natural loops.
 *
 * <p>The transform expects LoopSimplify-style dedicated exits. It handles multiple exits by
 * constructing SSA PHIs in the outside region and processes inner loops before outer loops. If an
 * escaping definition is not available on every relevant exit path, the definition is left
 * unchanged instead of inventing an undefined value.
 */
public final class LCSSA {
  private LCSSA() {}

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    LoopAnalysis.Result loopInfo = fam.getResult(LoopAnalysis.class, function);
    DominatorTreeAnalysis.Result dominators =
        fam.getResult(DominatorTreeAnalysis.class, function);
    boolean changed = false;
    for (LoopAnalysis.Loop loop : loopInfo.loops()) {
      changed |= formForLoop(function, loop, dominators);
    }
    return changed;
  }

  private static boolean formForLoop(
      Function function,
      LoopAnalysis.Loop loop,
      DominatorTreeAnalysis.Result dominators) {
    List<BasicBlock> exits = collectDedicatedExits(function, loop);
    if (exits == null || exits.isEmpty()) return false;

    boolean changed = false;
    List<Instruction> definitions = new ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!loop.contains(block)) continue;
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.hasResult()) definitions.add(instruction);
      }
    }
    for (Instruction definition : definitions) {
      changed |= closeDefinition(definition, loop, exits, dominators);
    }
    return changed;
  }

  private static boolean closeDefinition(
      Instruction definition,
      LoopAnalysis.Loop loop,
      List<BasicBlock> exits,
      DominatorTreeAnalysis.Result dominators) {
    List<ExternalUse> uses = collectExternalUses(definition, loop, exits);
    if (uses.isEmpty()) return false;

    Map<BasicBlock, Set<BasicBlock>> reachableByExit = new IdentityHashMap<>();
    for (BasicBlock exit : exits) {
      reachableByExit.put(exit, collectOutsideReachable(exit, loop));
    }

    Set<BasicBlock> neededExits = new LinkedHashSet<>();
    for (ExternalUse use : uses) {
      BasicBlock location = use.location();
      for (BasicBlock exit : exits) {
        if (reachableByExit.get(exit).contains(location)) neededExits.add(exit);
      }
    }
    if (neededExits.isEmpty()) return false;

    for (BasicBlock exit : neededExits) {
      for (BasicBlock predecessor : exit.getPredecessors()) {
        if (!loop.contains(predecessor)
            || !dominators.dominates(definition.getParent(), predecessor)) {
          return false;
        }
      }
    }
    for (ExternalUse use : uses) {
      if (!allBackwardPathsReachExit(
          use.location(), loop, neededExits, unionReachable(neededExits, reachableByExit))) {
        return false;
      }
    }

    Map<BasicBlock, Value> exitValues = new IdentityHashMap<>();
    for (BasicBlock exit : neededExits) {
      Instruction phi = createPhiAtFront(exit, definition);
      for (BasicBlock predecessor : exit.getPredecessors()) {
        phi.addOperand(definition);
        phi.addOperand(predecessor);
      }
      exitValues.put(exit, phi);
    }

    Map<BasicBlock, Value> availableAtEnd = new IdentityHashMap<>(exitValues);
    for (ExternalUse use : uses) {
      Value replacement =
          valueAtEnd(
              use.location(), definition, loop, exitValues, availableAtEnd, dominators);
      use.user().setOperand(use.operandIndex(), replacement);
    }
    return true;
  }

  private static List<ExternalUse> collectExternalUses(
      Instruction definition, LoopAnalysis.Loop loop, List<BasicBlock> exits) {
    Set<BasicBlock> exitSet = Set.copyOf(exits);
    List<ExternalUse> result = new ArrayList<>();
    for (Use use : new ArrayList<>(definition.getUses())) {
      Instruction user = use.getUser();
      if (loop.contains(user.getParent())) continue;
      int operandIndex = use.getOperandIndex();
      if (user.getOpcode() == Instruction.Opcode.PHI) {
        if ((operandIndex & 1) != 0 || operandIndex + 1 >= user.getNumOperands()) return List.of();
        BasicBlock incoming = (BasicBlock) user.getOperand(operandIndex + 1);
        // A PHI use on an edge into a dedicated exit already is in LCSSA form.
        if (exitSet.contains(user.getParent()) && loop.contains(incoming)) continue;
        result.add(new ExternalUse(user, operandIndex, incoming));
      } else {
        result.add(new ExternalUse(user, operandIndex, user.getParent()));
      }
    }
    return result;
  }

  private static Value valueAtEnd(
      BasicBlock block,
      Instruction definition,
      LoopAnalysis.Loop loop,
      Map<BasicBlock, Value> exitValues,
      Map<BasicBlock, Value> cache,
      DominatorTreeAnalysis.Result dominators) {
    Value cached = cache.get(block);
    if (cached != null) return cached;

    BasicBlock dominatingExit = null;
    for (BasicBlock exit : exitValues.keySet()) {
      if (!dominators.dominates(exit, block)) continue;
      if (dominatingExit == null || dominators.dominates(dominatingExit, exit)) {
        dominatingExit = exit;
      }
    }
    if (dominatingExit != null) {
      Value value = exitValues.get(dominatingExit);
      cache.put(block, value);
      return value;
    }

    // Install the PHI before recursion so cycles in the outside region are representable.
    Instruction phi = createPhiAtFront(block, definition);
    cache.put(block, phi);
    for (BasicBlock predecessor : block.getPredecessors()) {
      Value incoming =
          loop.contains(predecessor)
              ? definition
              : valueAtEnd(
                  predecessor, definition, loop, exitValues, cache, dominators);
      phi.addOperand(incoming);
      phi.addOperand(predecessor);
    }
    return phi;
  }

  private static Instruction createPhiAtFront(BasicBlock block, Instruction definition) {
    Instruction phi = Instruction.createPhi(definition.getType());
    if (definition.getName() != null) phi.setName(definition.getName() + ".lcssa");
    block.addInstructionToFront(phi);
    return phi;
  }

  /**
   * Returns null for a non-canonical loop. An exit is dedicated exactly when all of its
   * predecessors are in the loop.
   */
  private static List<BasicBlock> collectDedicatedExits(
      Function function, LoopAnalysis.Loop loop) {
    Set<BasicBlock> exits = new LinkedHashSet<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!loop.contains(block)) continue;
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)) exits.add(successor);
      }
    }
    for (BasicBlock exit : exits) {
      if (exit.getPredecessors().stream().anyMatch(predecessor -> !loop.contains(predecessor))) {
        return null;
      }
    }
    return List.copyOf(exits);
  }

  private static Set<BasicBlock> collectOutsideReachable(
      BasicBlock exit, LoopAnalysis.Loop loop) {
    Set<BasicBlock> reachable = new LinkedHashSet<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>();
    worklist.add(exit);
    while (!worklist.isEmpty()) {
      BasicBlock block = worklist.removeFirst();
      if (loop.contains(block) || !reachable.add(block)) continue;
      worklist.addAll(block.getSuccessors());
    }
    return reachable;
  }

  private static Set<BasicBlock> unionReachable(
      Set<BasicBlock> exits, Map<BasicBlock, Set<BasicBlock>> reachableByExit) {
    Set<BasicBlock> result = new LinkedHashSet<>();
    for (BasicBlock exit : exits) result.addAll(reachableByExit.get(exit));
    return result;
  }

  private static boolean allBackwardPathsReachExit(
      BasicBlock start,
      LoopAnalysis.Loop loop,
      Set<BasicBlock> exits,
      Set<BasicBlock> reachable) {
    Set<BasicBlock> visited = new LinkedHashSet<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>();
    worklist.add(start);
    while (!worklist.isEmpty()) {
      BasicBlock block = worklist.removeFirst();
      if (exits.contains(block) || !visited.add(block)) continue;
      if (loop.contains(block) || !reachable.contains(block)) return false;
      List<BasicBlock> predecessors = block.getPredecessors();
      if (predecessors.isEmpty()) return false;
      for (BasicBlock predecessor : predecessors) {
        if (!reachable.contains(predecessor)) return false;
        worklist.add(predecessor);
      }
    }
    return true;
  }

  private record ExternalUse(Instruction user, int operandIndex, BasicBlock location) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
