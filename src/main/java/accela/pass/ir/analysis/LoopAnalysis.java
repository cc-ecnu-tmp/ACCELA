package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Finds natural loops formed by a single dominating backedge. */
public final class LoopAnalysis implements FunctionAnalysis<LoopAnalysis.Result> {
  public record Loop(
      BasicBlock header, BasicBlock latch, BasicBlock preheader, Set<BasicBlock> blocks) {}

  public record Result(List<Loop> loops) {}

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    DominatorTreeAnalysis.Result dominators =
        fam.getResult(DominatorTreeAnalysis.class, function);
    Map<BasicBlock, List<BasicBlock>> predecessors = collectPredecessors(function);
    List<Loop> loops = new ArrayList<>();
    for (BasicBlock latch : function.getBlocks()) {
      for (BasicBlock header : latch.getSuccessors()) {
        if (!dominators.dominates(header, latch)) continue;
        Set<BasicBlock> blocks = naturalLoop(header, latch, predecessors);
        List<BasicBlock> outside = predecessors.get(header).stream()
            .filter(block -> !blocks.contains(block)).toList();
        BasicBlock preheader = outside.size() == 1 ? outside.get(0) : null;
        loops.add(new Loop(header, latch, preheader, Set.copyOf(blocks)));
      }
    }
    return new Result(List.copyOf(loops));
  }

  private static Set<BasicBlock> naturalLoop(
      BasicBlock header,
      BasicBlock latch,
      Map<BasicBlock, List<BasicBlock>> predecessors) {
    Set<BasicBlock> blocks = new LinkedHashSet<>();
    blocks.add(header);
    blocks.add(latch);
    Deque<BasicBlock> worklist = new ArrayDeque<>();
    if (latch != header) worklist.add(latch);
    while (!worklist.isEmpty()) {
      for (BasicBlock predecessor : predecessors.get(worklist.removeFirst())) {
        if (blocks.add(predecessor) && predecessor != header) worklist.add(predecessor);
      }
    }
    return blocks;
  }

  private static Map<BasicBlock, List<BasicBlock>> collectPredecessors(Function function) {
    Map<BasicBlock, List<BasicBlock>> predecessors = new IdentityHashMap<>();
    for (BasicBlock block : function.getBlocks()) predecessors.put(block, new ArrayList<>());
    for (BasicBlock block : function.getBlocks()) {
      for (BasicBlock successor : block.getSuccessors()) predecessors.get(successor).add(block);
    }
    return predecessors;
  }
}
