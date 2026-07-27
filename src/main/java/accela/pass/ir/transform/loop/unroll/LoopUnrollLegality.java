package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;

/** Structural and code-size checks required by the full-loop cloner. */
final class LoopUnrollLegality {
  private static final int MAX_UNROLLED_INSTRUCTIONS = 1600;

  private LoopUnrollLegality() {}

  static boolean isSafe(Function function, LoopUnrollCandidate candidate) {
    var loop = candidate.loop();
    var latch = candidate.induction().latch();
    Instruction terminator = latch.getTerminator();
    return instructionCount(candidate) * candidate.tripCount()
        <= MAX_UNROLLED_INSTRUCTIONS
        && loop.latches().size() == 1
        && loop.header() != latch
        && terminator != null
        && terminator.getOpcode() == Instruction.Opcode.BR
        && terminator.getOperand(0) == loop.header()
        && hasCanonicalHeaderPhis(candidate)
        && hasOnlyCanonicalExit(candidate)
        && !startsWithPhi(candidate.exit())
        && !containsCall(candidate)
        && !hasUnsupportedExternalUse(candidate)
        && function.getBlocks().containsAll(loop.blocks());
  }

  private static int instructionCount(LoopUnrollCandidate candidate) {
    return candidate.loop().blocks().stream()
        .mapToInt(block -> block.getInstructions().size())
        .sum();
  }

  private static boolean hasCanonicalHeaderPhis(LoopUnrollCandidate candidate) {
    for (Instruction phi : candidate.loop().header().getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      if (phi.getNumOperands() != 4
          || !hasIncoming(phi, candidate.induction().predecessor())
          || !hasIncoming(phi, candidate.induction().latch())) return false;
    }
    return true;
  }

  private static boolean hasIncoming(Instruction phi, BasicBlock predecessor) {
    for (int index = 1; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index) == predecessor) return true;
    }
    return false;
  }

  private static boolean hasOnlyCanonicalExit(LoopUnrollCandidate candidate) {
    int exits = 0;
    for (BasicBlock block : candidate.loop().blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (candidate.loop().contains(successor)) continue;
        if (block != candidate.loop().header() || successor != candidate.exit()) {
          return false;
        }
        exits++;
      }
    }
    return exits == 1;
  }

  private static boolean startsWithPhi(BasicBlock block) {
    return !block.getInstructions().isEmpty()
        && block.getInstructions().getFirst().getOpcode() == Instruction.Opcode.PHI;
  }

  private static boolean containsCall(LoopUnrollCandidate candidate) {
    return candidate.loop().blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
  }

  private static boolean hasUnsupportedExternalUse(LoopUnrollCandidate candidate) {
    for (BasicBlock block : candidate.loop().blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        for (var use : instruction.getUses()) {
          if (!candidate.loop().contains(use.getUser().getParent())
              && (block != candidate.loop().header()
                  || instruction.getOpcode() != Instruction.Opcode.PHI)) return true;
        }
      }
    }
    return false;
  }
}
