package accela.pass.ir.transform.loop.deletion;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;

/**
 * Conservatively removes canonical loops whose execution cannot affect the program.
 *
 * <p>Zero-trip header-tested loops may have repairable PHI live-outs. All deleted loops must be
 * free of observable or potentially trapping instructions; nonzero loops additionally require a
 * finite SCEV count and no loop-defined value used outside the loop.
 */
public final class LoopDeletion {
  private LoopDeletion() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    // Deliberately delete at most one loop per pipeline visit. Reanalyzing and repeatedly deleting
    // in the same pass made later candidates depend on CFG/PHI cleanup performed for an earlier
    // loop, which is too aggressive for this conservative transform.
    boolean changed = deleteOne(function, fam);
    if (changed) fam.invalidate(function, PreservedAnalyses.none());
    return changed;
  }

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    return run(function, fam);
  }

  private static boolean deleteOne(Function function, FunctionAnalysisManager fam) {
    LoopAnalysis.Result loops = fam.getResult(LoopAnalysis.class, function);
    ScalarEvolutionAnalysis.Result scev =
        fam.getResult(ScalarEvolutionAnalysis.class, function);
    for (LoopAnalysis.Loop loop : loops.loops()) {
      Candidate candidate = match(loop);
      if (candidate == null) continue;
      BigInteger count = scev.getConstantBackedgeTakenCount(loop).orElse(null);
      boolean zeroTrip = isZeroTrip(candidate, count);
      if (zeroTrip) {
        // The header still executes once in a zero-trip, header-tested loop. Requiring the whole
        // loop to be effect-free is deliberately stricter than necessary, but prevents deleting a
        // trapping/effectful header without needing block-level must-execute reasoning.
        if (!isEffectFree(loop) || !canRepairZeroTrip(candidate)) continue;
      } else if (count == null || !isEffectFree(loop) || hasLiveOut(loop)) {
        continue;
      }
      delete(function, candidate, zeroTrip);
      return true;
    }
    return false;
  }

  private static Candidate match(LoopAnalysis.Loop loop) {
    BasicBlock preheader = loop.preheader();
    if (preheader == null
        || loop.latches().size() != 1
        || preheader.getSuccessors().size() != 1
        || preheader.getSuccessors().getFirst() != loop.header()) {
      return null;
    }
    BasicBlock exiting = null;
    BasicBlock exit = null;
    int exitEdges = 0;
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (loop.contains(successor)) continue;
        exiting = block;
        exit = successor;
        exitEdges++;
      }
    }
    if (exitEdges != 1
        || exit.getPredecessors().stream().anyMatch(pred -> !loop.contains(pred))) {
      return null;
    }
    return new Candidate(loop, preheader, exiting, exit);
  }

  private static boolean isZeroTrip(Candidate candidate, BigInteger count) {
    if (candidate.exiting() != candidate.loop().header()) return false;
    Instruction branch = candidate.exiting().getTerminator();
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR) return false;
    boolean trueExits = branch.getOperand(1) == candidate.exit();
    boolean falseExits = branch.getOperand(2) == candidate.exit();
    if (trueExits == falseExits) return false;
    if (branch.getOperand(0) instanceof Constant.Int condition) {
      return condition.value != 0 ? trueExits : falseExits;
    }
    return BigInteger.ZERO.equals(count);
  }

  private static boolean isEffectFree(LoopAnalysis.Loop loop) {
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        switch (instruction.getOpcode()) {
          case LOAD, STORE, CALL, ALLOCA, SDIV, SREM, FDIV, RET -> {
            return false;
          }
          default -> {
            // Remaining opcodes are pure, non-trapping SSA or control flow in ACCELA IR.
          }
        }
      }
    }
    return true;
  }

  private static boolean canRepairZeroTrip(Candidate candidate) {
    for (BasicBlock block : candidate.loop().blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        for (Use use : instruction.getUses()) {
          Instruction user = use.getUser();
          if (candidate.loop().contains(user.getParent())) continue;
          if (instruction.getOpcode() != Instruction.Opcode.PHI
              || instruction.getParent() != candidate.loop().header()
              || user.getParent() != candidate.exit()
              || user.getOpcode() != Instruction.Opcode.PHI
              || (use.getOperandIndex() & 1) != 0
              || user.getOperand(use.getOperandIndex() + 1) != candidate.exiting()) {
            return false;
          }
        }
      }
    }
    return true;
  }

  private static boolean hasLiveOut(LoopAnalysis.Loop loop) {
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getUses().stream()
            .anyMatch(use -> !loop.contains(use.getUser().getParent()))) {
          return true;
        }
      }
    }
    return false;
  }

  private static void delete(Function function, Candidate candidate, boolean zeroTrip) {
    if (zeroTrip) {
      for (Instruction phi : candidate.loop().header().getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value start = incomingValue(phi, candidate.preheader());
        if (start == null) throw new IllegalStateException("canonical header PHI lacks entry value");
        phi.replaceAllUsesWith(start);
      }
    }

    retarget(candidate.preheader(), candidate.loop().header(), candidate.exit());
    for (Instruction phi : new ArrayList<>(candidate.exit().getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      for (int index = 1; index < phi.getNumOperands(); index += 2) {
        if (phi.getOperand(index) == candidate.exiting()) {
          phi.setOperand(index, candidate.preheader());
        }
      }
      if (phi.getNumOperands() == 2) {
        phi.replaceAllUsesWith(phi.getOperand(0));
        phi.eraseFromParent();
      }
    }

    List<BasicBlock> blocks =
        function.getBlocks().stream().filter(candidate.loop()::contains).toList();
    List<Instruction> instructions = new ArrayList<>();
    for (BasicBlock block : blocks) instructions.addAll(block.getInstructions());
    for (Instruction instruction : instructions) instruction.dropAllReferences();
    for (Instruction instruction : instructions) instruction.eraseFromParent();
    for (BasicBlock block : blocks) function.removeBlock(block);
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static void retarget(BasicBlock block, BasicBlock oldTarget, BasicBlock target) {
    Instruction terminator = block.getTerminator();
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == oldTarget) terminator.setOperand(index, target);
    }
  }

  private record Candidate(
      LoopAnalysis.Loop loop, BasicBlock preheader, BasicBlock exiting, BasicBlock exit) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopDeletion.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
