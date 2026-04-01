package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Computes forward dominator-tree information for one function.
 */
public final class DominatorTreeAnalysis
    implements FunctionAnalysis<DominatorTreeAnalysis.Result> {

  /** Immutable dominator-tree related data for one function. */
  public static final class Result {
    private final BasicBlock entry;
    private final Map<BasicBlock, BasicBlock> idom;
    private final Map<BasicBlock, List<BasicBlock>> children;
    private final Map<BasicBlock, Set<BasicBlock>> frontier;

    private Result(
        BasicBlock entry,
        Map<BasicBlock, BasicBlock> idom,
        Map<BasicBlock, List<BasicBlock>> children,
        Map<BasicBlock, Set<BasicBlock>> frontier) {
      this.entry = entry;
      this.idom = idom;
      this.children = children;
      this.frontier = frontier;
    }

    public BasicBlock getEntryBlock() {
      return entry;
    }

    public BasicBlock getImmediateDominator(BasicBlock block) {
      return idom.get(block);
    }

    public List<BasicBlock> getChildren(BasicBlock block) {
      return children.getOrDefault(block, List.of());
    }

    public Set<BasicBlock> getDominanceFrontier(BasicBlock block) {
      return frontier.getOrDefault(block, Set.of());
    }

    /** Returns whether {@code dominator} dominates {@code block}. */
    public boolean dominates(BasicBlock dominator, BasicBlock block) {
      if (dominator == null || block == null) return false;
      if (dominator == block) return true;
      BasicBlock cur = block;
      while (cur != null && cur != entry) {
        cur = idom.get(cur);
        if (cur == dominator) return true;
      }
      return dominator == entry && block == entry;
    }
  }

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    List<BasicBlock> blocks = function.getBlocks();
    BasicBlock entry = function.getEntryBlock();
    if (entry == null || blocks.isEmpty()) {
      return new Result(null, Map.of(), Map.of(), Map.of());
    }

    Map<BasicBlock, List<BasicBlock>> predecessors = collectPredecessors(blocks);
    List<BasicBlock> rpo = reversePostOrder(entry);
    Map<BasicBlock, Integer> rpoIndex = new IdentityHashMap<>();
    Map<BasicBlock, BasicBlock> idom = new LinkedHashMap<>();
    Map<BasicBlock, List<BasicBlock>> children = new LinkedHashMap<>();
    Map<BasicBlock, Set<BasicBlock>> frontier = new LinkedHashMap<>();

    for (BasicBlock bb : blocks) {
      children.put(bb, new ArrayList<>());
      frontier.put(bb, new LinkedHashSet<>());
    }
    for (int i = 0; i < rpo.size(); i++) {
      rpoIndex.put(rpo.get(i), i);
    }

    idom.put(entry, entry);
    boolean changed;
    do {
      changed = false;
      for (BasicBlock bb : rpo) {
        if (bb == entry) continue;
        List<BasicBlock> preds = predecessors.getOrDefault(bb, List.of());
        BasicBlock newIdom = null;
        for (BasicBlock pred : preds) {
          if (idom.get(pred) == null) continue;
          newIdom = (newIdom == null) ? pred : intersect(pred, newIdom, idom, rpoIndex);
        }
        if (newIdom != null && idom.get(bb) != newIdom) {
          idom.put(bb, newIdom);
          changed = true;
        }
      }
    } while (changed);

    for (BasicBlock bb : blocks) {
      if (bb == entry) continue;
      BasicBlock bbIdom = idom.get(bb);
      if (bbIdom != null) children.get(bbIdom).add(bb);
    }

    for (BasicBlock bb : blocks) {
      List<BasicBlock> preds = predecessors.getOrDefault(bb, List.of());
      if (preds.size() < 2) continue;
      BasicBlock bbIdom = idom.get(bb);
      for (BasicBlock pred : preds) {
        BasicBlock runner = pred;
        while (runner != null && runner != bbIdom) {
          frontier.get(runner).add(bb);
          BasicBlock next = idom.get(runner);
          if (next == runner) break;
          runner = next;
        }
      }
    }

    return new Result(entry, idom, children, frontier);
  }

  private static Map<BasicBlock, List<BasicBlock>> collectPredecessors(List<BasicBlock> blocks) {
    Map<BasicBlock, List<BasicBlock>> predecessors = new LinkedHashMap<>();
    for (BasicBlock bb : blocks) {
      predecessors.put(bb, new ArrayList<>());
    }
    for (BasicBlock bb : blocks) {
      for (BasicBlock succ : bb.getSuccessors()) {
        predecessors.computeIfAbsent(succ, ignored -> new ArrayList<>()).add(bb);
      }
    }
    return predecessors;
  }

  private static List<BasicBlock> reversePostOrder(BasicBlock entry) {
    List<BasicBlock> postOrder = new ArrayList<>();
    Set<BasicBlock> visited = new LinkedHashSet<>();
    buildPostOrder(entry, visited, postOrder);
    Collections.reverse(postOrder);
    return postOrder;
  }

  private static void buildPostOrder(
      BasicBlock bb, Set<BasicBlock> visited, List<BasicBlock> postOrder) {
    if (!visited.add(bb)) return;
    for (BasicBlock succ : bb.getSuccessors()) {
      buildPostOrder(succ, visited, postOrder);
    }
    postOrder.add(bb);
  }

  private static BasicBlock intersect(
      BasicBlock first,
      BasicBlock second,
      Map<BasicBlock, BasicBlock> idom,
      Map<BasicBlock, Integer> rpoIndex) {
    BasicBlock finger1 = first;
    BasicBlock finger2 = second;
    while (finger1 != finger2) {
      while (rpoIndex.get(finger1) > rpoIndex.get(finger2)) {
        finger1 = idom.get(finger1);
      }
      while (rpoIndex.get(finger2) > rpoIndex.get(finger1)) {
        finger2 = idom.get(finger2);
      }
    }
    return finger1;
  }
}
