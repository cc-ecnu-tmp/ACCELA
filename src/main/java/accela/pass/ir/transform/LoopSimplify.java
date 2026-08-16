package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Canonicalizes natural loops by forming preheaders, unique latch blocks, and dedicated exits.
 *
 * <p>Only natural-loop shapes represented by {@link LoopAnalysis} are changed. The pass restarts
 * loop analysis after every CFG edit so that nested loops are never rewritten using stale block
 * sets.
 */
public final class LoopSimplify {
  private LoopSimplify() {}

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    while (true) {
      boolean localChange = false;
      for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
        if (loop.preheader() == null && insertPreheader(function, loop)) {
          localChange = true;
          break;
        }
        if (loop.latches().size() > 1 && insertUniqueBackedge(function, loop)) {
          localChange = true;
          break;
        }
        if (insertDedicatedExit(function, loop)) {
          localChange = true;
          break;
        }
      }
      if (!localChange) return changed;
      changed = true;
      fam.invalidate(function, PreservedAnalyses.none());
    }
  }

  private static boolean insertPreheader(Function function, LoopAnalysis.Loop loop) {
    BasicBlock header = loop.header();
    List<BasicBlock> outside =
        function.getBlocks().stream()
            .filter(block -> !loop.contains(block) && block.getSuccessors().contains(header))
            .toList();
    // A loop whose header is the function entry has no representable incoming value for a
    // preheader. Leave that uncommon shape alone.
    if (outside.isEmpty() || !hasIncomingPairsForAllHeaderPhis(header, outside)) return false;

    BasicBlock preheader =
        function.insertBlockAfter(
            outside.getLast(), header.getLabel() + ".preheader." + function.getBlocks().size());
    IRBuilder builder = new IRBuilder(preheader);
    Set<BasicBlock> outsideSet = Set.copyOf(outside);
    for (Instruction phi : new ArrayList<>(header.getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value entryValue = mergeIncomingValues(builder, phi, outside);
      List<Value> loopPairs = incomingPairsExcept(phi, outsideSet);
      phi.clearAllOperands();
      loopPairs.forEach(phi::addOperand);
      phi.addOperand(entryValue);
      phi.addOperand(preheader);
    }
    builder.createBr(header);
    for (BasicBlock predecessor : outside) {
      replaceSuccessor(predecessor, header, preheader);
    }
    return true;
  }

  private static boolean insertUniqueBackedge(Function function, LoopAnalysis.Loop loop) {
    BasicBlock header = loop.header();
    Set<BasicBlock> latchSet = loop.latches();
    List<BasicBlock> latches =
        function.getBlocks().stream().filter(latchSet::contains).toList();
    if (latches.size() < 2 || !hasIncomingPairsForAllHeaderPhis(header, latches)) return false;

    BasicBlock backedge =
        function.insertBlockAfter(
            latches.getLast(), header.getLabel() + ".backedge." + function.getBlocks().size());
    IRBuilder builder = new IRBuilder(backedge);

    for (Instruction phi : new ArrayList<>(header.getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value backedgeValue = mergeIncomingValues(builder, phi, latches);
      List<Value> outside = incomingPairsExcept(phi, latchSet);
      phi.clearAllOperands();
      outside.forEach(phi::addOperand);
      phi.addOperand(backedgeValue);
      phi.addOperand(backedge);
    }
    builder.createBr(header);
    for (BasicBlock latch : latches) replaceSuccessor(latch, header, backedge);
    return true;
  }

  /**
   * Splits all edges from one loop to an exit that also has non-loop predecessors.
   *
   * <p>Combining the loop edges into one block is sufficient to make the new exit dedicated and
   * avoids creating a chain of one block per edge. Existing exit PHIs are merged in that block.
   */
  private static boolean insertDedicatedExit(Function function, LoopAnalysis.Loop loop) {
    for (BasicBlock exit : collectExitBlocks(function, loop)) {
      List<BasicBlock> predecessors = exit.getPredecessors();
      List<BasicBlock> exiting =
          predecessors.stream().filter(loop::contains).toList();
      if (exiting.isEmpty() || exiting.size() == predecessors.size()) continue;
      if (!hasIncomingPairsForAllHeaderPhis(exit, exiting)) continue;

      BasicBlock dedicated =
          function.insertBlockAfter(
              exiting.getLast(), loop.header().getLabel() + ".exit." + function.getBlocks().size());
      IRBuilder builder = new IRBuilder(dedicated);
      Set<BasicBlock> exitingSet = Set.copyOf(exiting);
      for (Instruction phi : new ArrayList<>(exit.getInstructions())) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        Value loopValue = mergeIncomingValues(builder, phi, exiting);
        List<Value> otherPairs = incomingPairsExcept(phi, exitingSet);
        phi.clearAllOperands();
        otherPairs.forEach(phi::addOperand);
        phi.addOperand(loopValue);
        phi.addOperand(dedicated);
      }
      builder.createBr(exit);
      for (BasicBlock predecessor : exiting) {
        replaceSuccessor(predecessor, exit, dedicated);
      }
      return true;
    }
    return false;
  }

  private static List<BasicBlock> collectExitBlocks(
      Function function, LoopAnalysis.Loop loop) {
    Set<BasicBlock> exits = new LinkedHashSet<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!loop.contains(block)) continue;
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)) exits.add(successor);
      }
    }
    return List.copyOf(exits);
  }

  private static boolean hasIncomingPairsForAllHeaderPhis(
      BasicBlock block, List<BasicBlock> predecessors) {
    for (Instruction phi : block.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      for (BasicBlock predecessor : predecessors) {
        if (!hasIncomingValue(phi, predecessor)) return false;
      }
    }
    return true;
  }

  private static boolean hasIncomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 1; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index) == predecessor) return true;
    }
    return false;
  }

  private static Value mergeIncomingValues(
      IRBuilder builder, Instruction headerPhi, List<BasicBlock> latches) {
    List<Value> values = new ArrayList<>();
    for (BasicBlock latch : latches) values.add(incomingValue(headerPhi, latch));
    if (values.stream().allMatch(value -> value == values.getFirst())) return values.getFirst();

    Instruction phi = builder.createPhi(headerPhi.getType());
    for (int index = 0; index < latches.size(); index++) {
      phi.addOperand(values.get(index));
      phi.addOperand(latches.get(index));
    }
    return phi;
  }

  private static List<Value> incomingPairsExcept(
      Instruction phi, Set<BasicBlock> excluded) {
    List<Value> result = new ArrayList<>();
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (excluded.contains((BasicBlock) phi.getOperand(index + 1))) continue;
      result.add(phi.getOperand(index));
      result.add(phi.getOperand(index + 1));
    }
    return result;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    throw new IllegalStateException("loop PHI has no latch value");
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
      return runOnFunction(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
