package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/**
 * Removes only redundant artifacts introduced by LoopSimplify and LCSSA.
 *
 * <p>This is intentionally narrower than SimplifyCFG. Legacy unrollers need PHI-free exits and do
 * not see through empty dedicated-exit blocks, but running general CFG cleanup before them changes
 * unrelated branch shapes and code generation.
 */
public final class LoopCanonicalizationCleanup {
  private LoopCanonicalizationCleanup() {}

  public static boolean runOnFunction(Function function) {
    boolean changed = false;
    boolean localChange;
    do {
      localChange = foldTrivialPhis(function);
      localChange |= bypassOneEmptyCanonicalBlock(function);
      changed |= localChange;
    } while (localChange);
    return changed;
  }

  private static boolean foldTrivialPhis(Function function) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction phi : new ArrayList<>(block.getInstructions())) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value common = null;
        boolean trivial = true;
        for (int index = 0; index < phi.getNumOperands(); index += 2) {
          Value incoming = phi.getOperand(index);
          if (incoming == phi) continue;
          if (common == null) common = incoming;
          else if (incoming != common) {
            trivial = false;
            break;
          }
        }
        if (!trivial || common == null) continue;
        phi.replaceAllUsesWith(common);
        phi.eraseFromParent();
        changed = true;
      }
    }
    return changed;
  }

  private static boolean bypassOneEmptyCanonicalBlock(Function function) {
    for (BasicBlock block : List.copyOf(function.getBlocks())) {
      if (block == function.getEntryBlock()
          || !isGeneratedCanonicalBlock(block)
          || block.getInstructions().stream()
              .limit(Math.max(0, block.getInstructions().size() - 1))
              .anyMatch(instruction -> instruction.getOpcode() != Instruction.Opcode.PHI)) {
        continue;
      }
      Instruction branch = block.getTerminator();
      if (branch == null || branch.getOpcode() != Instruction.Opcode.BR) continue;
      BasicBlock successor = (BasicBlock) branch.getOperand(0);
      List<BasicBlock> predecessors = block.getPredecessors();
      if (predecessors.isEmpty()
          || predecessors.stream().anyMatch(
              predecessor -> predecessor.getSuccessors().contains(successor))) continue;
      List<Instruction> bridgePhis =
          block.getInstructions().stream()
              .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
              .toList();
      if (bridgePhis.stream().flatMap(phi -> phi.getUses().stream())
          .anyMatch(use -> use.getUser().getParent() != successor
              || use.getUser().getOpcode() != Instruction.Opcode.PHI
              || (use.getOperandIndex() & 1) != 0
              || use.getOperandIndex() + 1 >= use.getUser().getNumOperands()
              || use.getUser().getOperand(use.getOperandIndex() + 1) != block)) continue;

      for (Instruction phi : new ArrayList<>(successor.getInstructions())) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value incoming = incomingValue(phi, block);
        if (incoming == null) return false;
        List<Value> pairs = incomingPairsExcept(phi, block);
        for (BasicBlock predecessor : predecessors) {
          Value expanded =
              incoming instanceof Instruction bridgePhi && bridgePhis.contains(bridgePhi)
                  ? incomingValue(bridgePhi, predecessor)
                  : incoming;
          if (expanded == null) return false;
          pairs.add(expanded);
          pairs.add(predecessor);
        }
        setPhiPairsInBlockOrder(function, phi, pairs);
      }
      for (BasicBlock predecessor : predecessors) {
        replaceSuccessor(predecessor, block, successor);
      }
      for (Instruction instruction : List.copyOf(block.getInstructions())) {
        instruction.eraseFromParent();
      }
      function.removeBlock(block);
      return true;
    }
    return false;
  }

  private static boolean isGeneratedCanonicalBlock(BasicBlock block) {
    return block.getLabel().matches(".*\\.(?:preheader|backedge|exit)\\.\\d+");
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static List<Value> incomingPairsExcept(Instruction phi, BasicBlock excluded) {
    List<Value> result = new ArrayList<>();
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == excluded) continue;
      result.add(phi.getOperand(index));
      result.add(phi.getOperand(index + 1));
    }
    return result;
  }

  private static void setPhiPairsInBlockOrder(
      Function function, Instruction phi, List<Value> operands) {
    Map<BasicBlock, Integer> order = new IdentityHashMap<>();
    for (int index = 0; index < function.getBlocks().size(); index++) {
      order.put(function.getBlocks().get(index), index);
    }
    List<List<Value>> pairs = new ArrayList<>();
    for (int index = 0; index < operands.size(); index += 2) {
      pairs.add(List.of(operands.get(index), operands.get(index + 1)));
    }
    pairs.sort(Comparator.comparingInt(
        pair -> order.getOrDefault((BasicBlock) pair.get(1), Integer.MAX_VALUE)));
    phi.clearAllOperands();
    for (List<Value> pair : pairs) {
      phi.addOperand(pair.get(0));
      phi.addOperand(pair.get(1));
    }
  }

  private static void replaceSuccessor(
      BasicBlock block, BasicBlock oldTarget, BasicBlock newTarget) {
    Instruction terminator = block.getTerminator();
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == oldTarget) terminator.setOperand(index, newTarget);
    }
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
