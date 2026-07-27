package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;

/** Implements LICM scalar promotion for directly accessed global objects. */
final class LoopAccessPromotion {
  private LoopAccessPromotion() {}

  static boolean run(
      Function function,
      LoopAnalysis.Loop loop,
      GlobalModRefAnalysis.Result modRef,
      DominatorTreeAnalysis.Result dominators) {
    boolean changed = false;
    for (PromotionCandidate candidate : PromotionCandidate.find(loop, modRef)) {
      promote(function, loop, candidate, dominators);
      changed = true;
    }
    return changed;
  }

  private static void promote(
      Function function,
      LoopAnalysis.Loop loop,
      PromotionCandidate candidate,
      DominatorTreeAnalysis.Result dominators) {
    var global = candidate.global();
    IRBuilder builder = new IRBuilder();
    Instruction slot =
        builder.createAllocaInEntry(global.getValueType(), function.getEntryBlock());
    builder.setInsertPointBefore(loop.preheader().getTerminator());
    builder.createStore(builder.createLoad(global.getValueType(), global), slot);

    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        int pointerIndex = PromotionCandidate.pointerIndex(instruction);
        if (pointerIndex >= 0 && instruction.getOperand(pointerIndex) == global) {
          instruction.setOperand(pointerIndex, slot);
        }
      }
    }
    for (BasicBlock exit : candidate.exits()) {
      Instruction first = exit.getInstructions().stream()
          .filter(instruction -> instruction.getOpcode() != Instruction.Opcode.PHI)
          .findFirst()
          .orElseThrow();
      builder.setInsertPointBefore(first);
      builder.createStore(builder.createLoad(global.getValueType(), slot), global);
    }
    PromoteMemoryToRegister.promoteAlloca(function, slot, dominators);
  }
}
