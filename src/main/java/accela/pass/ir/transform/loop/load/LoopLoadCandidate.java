package accela.pass.ir.transform.loop.load;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;

/** A unit-distance store-to-load dependence carried by one pointer PHI. */
record LoopLoadCandidate(
    Instruction load,
    Instruction store,
    Instruction pointer,
    Value initialPointer,
    long loadOffset) {

  static LoopLoadCandidate match(LoopAnalysis.Loop loop) {
    BasicBlock memoryBlock = memoryBlock(loop);
    if (memoryBlock == null || containsCall(loop)) return null;
    Instruction store = onlyStore(loop);
    if (store == null || store.getParent() != memoryBlock) return null;
    LoopPointerAccess stored = LoopPointerAccess.match(store.getOperand(1), loop);
    if (stored == null) return null;

    for (Instruction instruction : memoryBlock.getInstructions()) {
      if (instruction.getOpcode() != Instruction.Opcode.LOAD
          || instruction.getType() != store.getOperand(0).getType()) continue;
      LoopPointerAccess loaded = LoopPointerAccess.match(instruction.getOperand(0), loop);
      if (loaded != null
          && loaded.pointer() == stored.pointer()
          && stored.offset() - loaded.offset() == loaded.step()) {
        return new LoopLoadCandidate(
            instruction, store, loaded.pointer(), loaded.initial(), loaded.offset());
      }
    }
    return null;
  }

  boolean isUnconditional(LoopAnalysis.Loop loop) {
    return load.getParent() == loop.header();
  }

  private static BasicBlock memoryBlock(LoopAnalysis.Loop loop) {
    if (loop.latches().size() != 1 || loop.blocks().size() > 2) return null;
    BasicBlock latch = loop.latches().iterator().next();
    if (loop.blocks().size() == 1) return latch == loop.header() ? latch : null;
    Instruction terminator = latch.getTerminator();
    return terminator != null
            && terminator.getOpcode() == Instruction.Opcode.BR
            && terminator.getOperand(0) == loop.header()
        ? latch : null;
  }

  private static Instruction onlyStore(LoopAnalysis.Loop loop) {
    Instruction result = null;
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() != Instruction.Opcode.STORE) continue;
        if (result != null) return null;
        result = instruction;
      }
    }
    return result;
  }

  private static boolean containsCall(LoopAnalysis.Loop loop) {
    return loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
  }

}
