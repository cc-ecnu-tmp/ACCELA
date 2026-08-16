package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Replaces repeated loop accesses to a stable scalar address with an SSA value. */
final class LoopAccessPromotion {
  private LoopAccessPromotion() {}

  static boolean run(
      Function function,
      LoopAnalysis.Loop loop,
      GlobalModRefAnalysis.Result modRef,
      DominatorTreeAnalysis.Result dominators) {
    List<PromotionCandidate> candidates = PromotionCandidate.find(loop, modRef, dominators);
    if (candidates.isEmpty()) return false;
    promote(function, loop, candidates);
    return true;
  }

  private static void promote(
      Function function,
      LoopAnalysis.Loop loop,
      List<PromotionCandidate> candidates) {
    IRBuilder builder = new IRBuilder();
    Map<PromotionCandidate, Instruction> slots = new LinkedHashMap<>();
    for (PromotionCandidate candidate : candidates) {
      Instruction slot =
          builder.createAllocaInEntry(candidate.valueType(), function.getEntryBlock());
      builder.setInsertPointBefore(loop.preheader().getTerminator());
      builder.createStore(
          builder.createLoad(candidate.valueType(), candidate.pointer()), slot);
      slots.put(candidate, slot);
    }

    for (BasicBlock block : PromotionCandidate.orderedBlocks(loop)) {
      for (Instruction instruction : block.getInstructions()) {
        int pointerIndex = PromotionCandidate.pointerIndex(instruction);
        if (pointerIndex < 0) continue;
        for (PromotionCandidate candidate : candidates) {
          if (instruction.getOperand(pointerIndex) == candidate.pointer()) {
            instruction.setOperand(pointerIndex, slots.get(candidate));
            break;
          }
        }
      }
    }

    List<PromotionCandidate.ExitEdge> exitEdges = candidates.getFirst().exitEdges();
    int edgeIndex = 0;
    for (PromotionCandidate.ExitEdge edge : exitEdges) {
      String baseLabel = loop.header().getLabel() + ".promote.exit." + edgeIndex++;
      BasicBlock writeback = function.insertBlockAfter(
          edge.predecessor(), uniqueBlockLabel(function, baseLabel));
      for (PromotionCandidate candidate : candidates) {
        builder.setInsertPoint(writeback);
        builder.createStore(
            builder.createLoad(candidate.valueType(), slots.get(candidate)),
            candidate.pointer());
      }
      builder.setInsertPoint(writeback);
      builder.createBr(edge.exit());
      retarget(edge.predecessor().getTerminator(), edge.exit(), writeback);
      replacePhiPredecessor(edge.exit(), edge.predecessor(), writeback);
    }
    // Splitting exit edges changes dominance. Mem2reg must see the updated CFG or it can leave
    // loads in the new writeback blocks referring to an alloca that it has already removed.
    DominatorTreeAnalysis.Result updatedDominators =
        new DominatorTreeAnalysis().run(function, null);
    for (Instruction slot : slots.values()) {
      PromoteMemoryToRegister.promoteAlloca(function, slot, updatedDominators);
    }
  }

  private static String uniqueBlockLabel(Function function, String base) {
    if (function.getBlocks().stream().noneMatch(block -> block.getLabel().equals(base))) {
      return base;
    }
    int suffix = 1;
    while (true) {
      String candidate = base + "." + suffix++;
      if (function.getBlocks().stream()
          .noneMatch(block -> block.getLabel().equals(candidate))) return candidate;
    }
  }

  private static void retarget(
      Instruction branch, BasicBlock oldTarget, BasicBlock newTarget) {
    boolean replaced = false;
    for (int index = 0; index < branch.getNumOperands(); index++) {
      if (branch.getOperand(index) == oldTarget) {
        branch.setOperand(index, newTarget);
        replaced = true;
      }
    }
    if (!replaced) throw new IllegalStateException("loop exit edge no longer exists");
  }

  private static void replacePhiPredecessor(
      BasicBlock exit, BasicBlock oldPredecessor, BasicBlock newPredecessor) {
    for (Instruction phi : exit.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      for (int index = 1; index < phi.getNumOperands(); index += 2) {
        if (phi.getOperand(index) == oldPredecessor) {
          phi.setOperand(index, newPredecessor);
        }
      }
    }
  }
}
