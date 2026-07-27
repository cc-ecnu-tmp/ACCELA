package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Finds reachable natural loops from backedges to dominating headers. */
public final class LoopAnalysis implements FunctionAnalysis<LoopAnalysis.Result> {
  public record Loop(
      BasicBlock header,
      Set<BasicBlock> latches,
      BasicBlock preheader,
      Set<BasicBlock> blocks) {
    public Loop {
      latches = Set.copyOf(latches);
      blocks = Set.copyOf(blocks);
    }

    public boolean contains(BasicBlock block) {
      return blocks.contains(block);
    }
  }

  public record Result(List<Loop> loops) {
    public Result {
      loops = List.copyOf(loops);
    }

    /** Returns the innermost natural loop containing {@code block}. */
    public Loop getLoopFor(BasicBlock block) {
      return loops.stream()
          .filter(loop -> loop.contains(block))
          .min(Comparator.comparingInt(loop -> loop.blocks().size()))
          .orElse(null);
    }
  }

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return new Result(List.of());

    DominatorTreeAnalysis.Result dominators =
        fam.getResult(DominatorTreeAnalysis.class, function);
    Map<BasicBlock, List<BasicBlock>> predecessors = collectPredecessors(function);
    Set<BasicBlock> reachable = collectReachable(entry);
    Map<BasicBlock, Set<BasicBlock>> latchesByHeader = new LinkedHashMap<>();
    for (BasicBlock latch : function.getBlocks()) {
      if (!reachable.contains(latch)) continue;
      for (BasicBlock header : latch.getSuccessors()) {
        if (reachable.contains(header) && dominators.dominates(header, latch)) {
          latchesByHeader
              .computeIfAbsent(header, ignored -> new LinkedHashSet<>())
              .add(latch);
        }
      }
    }

    List<Loop> loops = new ArrayList<>();
    for (var entrySet : latchesByHeader.entrySet()) {
      BasicBlock header = entrySet.getKey();
      Set<BasicBlock> blocks =
          collectNaturalLoop(header, entrySet.getValue(), predecessors, reachable, dominators);
      loops.add(
          new Loop(header, entrySet.getValue(), findPreheader(header, blocks, predecessors), blocks));
    }
    loops.sort(Comparator.comparingInt(loop -> loop.blocks().size()));
    return new Result(loops);
  }

  private static Set<BasicBlock> collectNaturalLoop(
      BasicBlock header,
      Set<BasicBlock> latches,
      Map<BasicBlock, List<BasicBlock>> predecessors,
      Set<BasicBlock> reachable,
      DominatorTreeAnalysis.Result dominators) {
    Set<BasicBlock> blocks = new LinkedHashSet<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>();
    blocks.add(header);
    for (BasicBlock latch : latches) {
      if (blocks.add(latch)) worklist.add(latch);
    }
    while (!worklist.isEmpty()) {
      for (BasicBlock predecessor : predecessors.get(worklist.removeFirst())) {
        if (reachable.contains(predecessor)
            && dominators.dominates(header, predecessor)
            && blocks.add(predecessor)) {
          worklist.add(predecessor);
        }
      }
    }
    return blocks;
  }

  private static BasicBlock findPreheader(
      BasicBlock header,
      Set<BasicBlock> blocks,
      Map<BasicBlock, List<BasicBlock>> predecessors) {
    List<BasicBlock> outside = predecessors.get(header).stream()
        .filter(block -> !blocks.contains(block))
        .toList();
    if (outside.size() != 1) return null;
    BasicBlock candidate = outside.getFirst();
    return candidate.getSuccessors().stream().allMatch(successor -> successor == header)
        ? candidate : null;
  }

  private static Map<BasicBlock, List<BasicBlock>> collectPredecessors(Function function) {
    Map<BasicBlock, List<BasicBlock>> predecessors = new IdentityHashMap<>();
    for (BasicBlock block : function.getBlocks()) predecessors.put(block, new ArrayList<>());
    for (BasicBlock block : function.getBlocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        predecessors.get(successor).add(block);
      }
    }
    return predecessors;
  }

  private static Set<BasicBlock> collectReachable(BasicBlock entry) {
    Set<BasicBlock> reachable = new LinkedHashSet<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>();
    worklist.add(entry);
    while (!worklist.isEmpty()) {
      BasicBlock block = worklist.removeFirst();
      if (!reachable.add(block)) continue;
      worklist.addAll(block.getSuccessors());
    }
    return reachable;
  }
}
