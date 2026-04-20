package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Computes post-dominator information for one function.
 *
 * <p>The analysis models a synthetic virtual exit/root. Real {@code ret} blocks and reachable
 * no-exit regions both conceptually flow to that root, which allows the result to stay defined for
 * all entry-reachable blocks while still exposing whether a block reaches some real function exit.
 */
public final class PostDominatorTreeAnalysis
    implements FunctionAnalysis<PostDominatorTreeAnalysis.Result> {
  private static final Object VIRTUAL_EXIT = new Object();

  /** Immutable post-dominator-tree related data for one function. */
  public static final class Result {
    private final Set<BasicBlock> reachableBlocks;
    private final Set<BasicBlock> exitReachableBlocks;
    private final List<BasicBlock> exitBlocks;
    private final Set<BasicBlock> solvedBlocks;
    private final Map<BasicBlock, BasicBlock> realIpdom;
    private final Set<BasicBlock> virtualExitIpdomBlocks;
    private final Map<BasicBlock, List<BasicBlock>> children;
    private final List<BasicBlock> virtualExitChildren;
    private final Map<BasicBlock, Set<BasicBlock>> frontier;

    private Result(
        Set<BasicBlock> reachableBlocks,
        Set<BasicBlock> exitReachableBlocks,
        List<BasicBlock> exitBlocks,
        Set<BasicBlock> solvedBlocks,
        Map<BasicBlock, BasicBlock> realIpdom,
        Set<BasicBlock> virtualExitIpdomBlocks,
        Map<BasicBlock, List<BasicBlock>> children,
        List<BasicBlock> virtualExitChildren,
        Map<BasicBlock, Set<BasicBlock>> frontier) {
      this.reachableBlocks = Collections.unmodifiableSet(new LinkedHashSet<>(reachableBlocks));
      this.exitReachableBlocks =
          Collections.unmodifiableSet(new LinkedHashSet<>(exitReachableBlocks));
      this.exitBlocks = Collections.unmodifiableList(new ArrayList<>(exitBlocks));
      this.solvedBlocks = Collections.unmodifiableSet(new LinkedHashSet<>(solvedBlocks));
      this.realIpdom = Collections.unmodifiableMap(new LinkedHashMap<>(realIpdom));
      this.virtualExitIpdomBlocks =
          Collections.unmodifiableSet(new LinkedHashSet<>(virtualExitIpdomBlocks));

      Map<BasicBlock, List<BasicBlock>> immutableChildren = new LinkedHashMap<>();
      for (Map.Entry<BasicBlock, List<BasicBlock>> entry : children.entrySet()) {
        immutableChildren.put(entry.getKey(), Collections.unmodifiableList(entry.getValue()));
      }
      this.children = Collections.unmodifiableMap(immutableChildren);
      this.virtualExitChildren =
          Collections.unmodifiableList(new ArrayList<>(virtualExitChildren));

      Map<BasicBlock, Set<BasicBlock>> immutableFrontier = new LinkedHashMap<>();
      for (Map.Entry<BasicBlock, Set<BasicBlock>> entry : frontier.entrySet()) {
        immutableFrontier.put(entry.getKey(), Collections.unmodifiableSet(entry.getValue()));
      }
      this.frontier = Collections.unmodifiableMap(immutableFrontier);
    }

    public Set<BasicBlock> getReachableBlocks() {
      return reachableBlocks;
    }

    public Set<BasicBlock> getExitReachableBlocks() {
      return exitReachableBlocks;
    }

    public List<BasicBlock> getExitBlocks() {
      return exitBlocks;
    }

    /** Returns whether the block is in the domain where this analysis defines post-dominator data. */
    public boolean isInSolvedDomain(BasicBlock block) {
      return solvedBlocks.contains(block);
    }

    public boolean isReachable(BasicBlock block) {
      return reachableBlocks.contains(block);
    }

    /** Returns whether the block has a path to some real function exit. */
    public boolean reachesExit(BasicBlock block) {
      return exitReachableBlocks.contains(block);
    }

    /**
     * Returns the nearest real post-dominator of {@code block}, or {@code null} when the block is
     * outside the solved domain or when its immediate post-dominator is the synthetic virtual exit.
     */
    public BasicBlock getImmediatePostDominator(BasicBlock block) {
      return realIpdom.get(block);
    }

    /**
     * Returns whether the block is in the solved domain and its immediate post-dominator is the
     * synthetic virtual exit rather than a real block.
     */
    public boolean hasVirtualExitImmediatePostDominator(BasicBlock block) {
      return virtualExitIpdomBlocks.contains(block);
    }

    public List<BasicBlock> getChildren(BasicBlock block) {
      return children.getOrDefault(block, List.of());
    }

    /** Returns the real blocks whose immediate post-dominator is the synthetic virtual exit. */
    public List<BasicBlock> getVirtualExitChildren() {
      return virtualExitChildren;
    }

    public Set<BasicBlock> getPostDominanceFrontier(BasicBlock block) {
      return frontier.getOrDefault(block, Set.of());
    }

    /** Returns whether {@code postDominator} post-dominates {@code block}. */
    public boolean postDominates(BasicBlock postDominator, BasicBlock block) {
      if (postDominator == null || block == null) return false;
      if (!solvedBlocks.contains(postDominator) || !solvedBlocks.contains(block)) {
        return false;
      }
      if (postDominator == block) return true;
      BasicBlock cur = block;
      while (cur != null) {
        cur = realIpdom.get(cur);
        if (cur == postDominator) return true;
        if (virtualExitIpdomBlocks.contains(cur)) break;
      }
      return false;
    }
  }

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    List<BasicBlock> blocks = function.getBlocks();
    BasicBlock entry = function.getEntryBlock();
    if (entry == null || blocks.isEmpty()) {
      return new Result(
          Set.of(), Set.of(), List.of(), Set.of(), Map.of(), Set.of(), Map.of(), List.of(), Map.of());
    }

    Set<BasicBlock> reachableBlocks = collectReachableBlocks(entry);
    List<BasicBlock> exitBlocks = collectExitBlocks(reachableBlocks);

    Map<BasicBlock, List<BasicBlock>> children = new LinkedHashMap<>();
    Map<BasicBlock, Set<BasicBlock>> frontier = new LinkedHashMap<>();
    for (BasicBlock bb : blocks) {
      children.put(bb, new ArrayList<>());
      frontier.put(bb, new LinkedHashSet<>());
    }

    Map<BasicBlock, List<BasicBlock>> predecessors = collectPredecessors(reachableBlocks);
    Set<BasicBlock> exitReachableBlocks = collectExitReachableBlocks(exitBlocks, predecessors);
    Set<BasicBlock> noExitVirtualExitAnchors =
        collectNoExitVirtualExitAnchors(reachableBlocks, exitReachableBlocks);
    Map<BasicBlock, List<Object>> effectiveSuccessors =
        buildEffectiveSuccessors(reachableBlocks, noExitVirtualExitAnchors);
    Map<BasicBlock, Object> ipdomNode =
        computeImmediatePostDominators(reachableBlocks, effectiveSuccessors);

    Set<BasicBlock> solvedBlocks = reachableBlocks;
    Map<BasicBlock, BasicBlock> realIpdom = new LinkedHashMap<>();
    Set<BasicBlock> virtualExitIpdomBlocks = new LinkedHashSet<>();
    List<BasicBlock> virtualExitChildren = new ArrayList<>();
    for (BasicBlock bb : reachableBlocks) {
      Object parent = ipdomNode.get(bb);
      if (parent == null) {
        continue;
      }
      if (parent == VIRTUAL_EXIT) {
        virtualExitIpdomBlocks.add(bb);
        virtualExitChildren.add(bb);
      } else {
        BasicBlock parentBlock = (BasicBlock) parent;
        realIpdom.put(bb, parentBlock);
        children.get(parentBlock).add(bb);
      }
    }
    computePostDominanceFrontier(reachableBlocks, effectiveSuccessors, ipdomNode, frontier);

    return new Result(
        reachableBlocks,
        exitReachableBlocks,
        exitBlocks,
        solvedBlocks,
        realIpdom,
        virtualExitIpdomBlocks,
        children,
        virtualExitChildren,
        frontier);
  }

  private static Set<BasicBlock> collectReachableBlocks(BasicBlock entry) {
    Set<BasicBlock> reachable = new LinkedHashSet<>();
    Deque<BasicBlock> work = new ArrayDeque<>();
    work.add(entry);
    while (!work.isEmpty()) {
      BasicBlock bb = work.removeFirst();
      if (!reachable.add(bb)) continue;
      for (BasicBlock succ : bb.getSuccessors()) {
        work.addLast(succ);
      }
    }
    return reachable;
  }

  private static List<BasicBlock> collectExitBlocks(Set<BasicBlock> reachableBlocks) {
    List<BasicBlock> exits = new ArrayList<>();
    for (BasicBlock bb : reachableBlocks) {
      Instruction term = bb.getTerminator();
      if (term != null && term.getOpcode() == Instruction.Opcode.RET) {
        exits.add(bb);
      }
    }
    return exits;
  }

  private static Map<BasicBlock, List<BasicBlock>> collectPredecessors(Set<BasicBlock> blocks) {
    Map<BasicBlock, List<BasicBlock>> predecessors = new LinkedHashMap<>();
    for (BasicBlock bb : blocks) {
      predecessors.put(bb, new ArrayList<>());
    }
    for (BasicBlock bb : blocks) {
      for (BasicBlock succ : bb.getSuccessors()) {
        if (!blocks.contains(succ)) continue;
        predecessors.computeIfAbsent(succ, ignored -> new ArrayList<>()).add(bb);
      }
    }
    return predecessors;
  }

  private static Set<BasicBlock> collectExitReachableBlocks(
      List<BasicBlock> exitBlocks, Map<BasicBlock, List<BasicBlock>> predecessors) {
    Set<BasicBlock> exitReachable = new LinkedHashSet<>();
    Deque<BasicBlock> work = new ArrayDeque<>(exitBlocks);
    while (!work.isEmpty()) {
      BasicBlock bb = work.removeFirst();
      if (!exitReachable.add(bb)) continue;
      for (BasicBlock pred : predecessors.getOrDefault(bb, List.of())) {
        work.addLast(pred);
      }
    }
    return exitReachable;
  }

  private static Map<BasicBlock, List<Object>> buildEffectiveSuccessors(
      Set<BasicBlock> reachableBlocks, Set<BasicBlock> noExitVirtualExitAnchors) {
    Map<BasicBlock, List<Object>> effectiveSuccessors = new LinkedHashMap<>();
    for (BasicBlock bb : reachableBlocks) {
      List<Object> succs = new ArrayList<>();
      for (BasicBlock succ : bb.getSuccessors()) {
        if (reachableBlocks.contains(succ)) {
          succs.add(succ);
        }
      }

      Instruction term = bb.getTerminator();
      boolean isRet = term != null && term.getOpcode() == Instruction.Opcode.RET;
      if (isRet || noExitVirtualExitAnchors.contains(bb)) {
        succs.add(VIRTUAL_EXIT);
      }
      if (succs.isEmpty()) {
        succs.add(VIRTUAL_EXIT);
      }
      effectiveSuccessors.put(bb, succs);
    }
    return effectiveSuccessors;
  }

  private static Set<BasicBlock> collectNoExitVirtualExitAnchors(
      Set<BasicBlock> reachableBlocks, Set<BasicBlock> exitReachableBlocks) {
    Set<BasicBlock> noExitBlocks = new LinkedHashSet<>(reachableBlocks);
    noExitBlocks.removeAll(exitReachableBlocks);
    if (noExitBlocks.isEmpty()) {
      return Set.of();
    }

    Map<BasicBlock, Integer> index = new LinkedHashMap<>();
    Map<BasicBlock, Integer> lowlink = new LinkedHashMap<>();
    Deque<BasicBlock> stack = new ArrayDeque<>();
    Set<BasicBlock> onStack = new LinkedHashSet<>();
    List<List<BasicBlock>> sccs = new ArrayList<>();
    int[] nextIndex = {0};

    for (BasicBlock bb : noExitBlocks) {
      if (!index.containsKey(bb)) {
        strongConnect(bb, noExitBlocks, index, lowlink, stack, onStack, sccs, nextIndex);
      }
    }

    Map<BasicBlock, Integer> sccIndex = new LinkedHashMap<>();
    for (int i = 0; i < sccs.size(); i++) {
      for (BasicBlock bb : sccs.get(i)) {
        sccIndex.put(bb, i);
      }
    }

    Set<Integer> bottomSccs = new LinkedHashSet<>();
    for (int i = 0; i < sccs.size(); i++) {
      boolean bottom = true;
      for (BasicBlock bb : sccs.get(i)) {
        for (BasicBlock succ : bb.getSuccessors()) {
          if (!noExitBlocks.contains(succ)) {
            continue;
          }
          if (!sccIndex.get(bb).equals(sccIndex.get(succ))) {
            bottom = false;
            break;
          }
        }
        if (!bottom) {
          break;
        }
      }
      if (bottom) {
        bottomSccs.add(i);
      }
    }

    Set<BasicBlock> anchors = new LinkedHashSet<>();
    for (Integer sccId : bottomSccs) {
      anchors.addAll(sccs.get(sccId));
    }
    return anchors;
  }

  private static void strongConnect(
      BasicBlock bb,
      Set<BasicBlock> noExitBlocks,
      Map<BasicBlock, Integer> index,
      Map<BasicBlock, Integer> lowlink,
      Deque<BasicBlock> stack,
      Set<BasicBlock> onStack,
      List<List<BasicBlock>> sccs,
      int[] nextIndex) {
    index.put(bb, nextIndex[0]);
    lowlink.put(bb, nextIndex[0]);
    nextIndex[0]++;
    stack.push(bb);
    onStack.add(bb);

    for (BasicBlock succ : bb.getSuccessors()) {
      if (!noExitBlocks.contains(succ)) {
        continue;
      }
      if (!index.containsKey(succ)) {
        strongConnect(succ, noExitBlocks, index, lowlink, stack, onStack, sccs, nextIndex);
        lowlink.put(bb, Math.min(lowlink.get(bb), lowlink.get(succ)));
      } else if (onStack.contains(succ)) {
        lowlink.put(bb, Math.min(lowlink.get(bb), index.get(succ)));
      }
    }

    if (!lowlink.get(bb).equals(index.get(bb))) {
      return;
    }

    List<BasicBlock> scc = new ArrayList<>();
    while (true) {
      BasicBlock member = stack.pop();
      onStack.remove(member);
      scc.add(member);
      if (member == bb) {
        break;
      }
    }
    sccs.add(scc);
  }

  private static Map<BasicBlock, Object> computeImmediatePostDominators(
      Set<BasicBlock> reachableBlocks, Map<BasicBlock, List<Object>> effectiveSuccessors) {
    Map<Object, List<Object>> reverseSuccessors =
        buildReverseSuccessors(reachableBlocks, effectiveSuccessors);
    List<Object> reversePostOrder = computeReversePostOrder(reverseSuccessors);
    Map<Object, Integer> order = new IdentityHashMap<>();
    for (int i = 0; i < reversePostOrder.size(); i++) {
      order.put(reversePostOrder.get(i), i);
    }

    Map<Object, Object> idom = new IdentityHashMap<>();
    idom.put(VIRTUAL_EXIT, VIRTUAL_EXIT);

    boolean changed;
    do {
      changed = false;
      for (Object node : reversePostOrder) {
        if (!(node instanceof BasicBlock bb)) {
          continue;
        }

        Object newIpdom = null;
        for (Object successor : effectiveSuccessors.getOrDefault(bb, List.of())) {
          if (idom.containsKey(successor)) {
            newIpdom = successor;
            break;
          }
        }
        if (newIpdom == null) {
          continue;
        }

        for (Object successor : effectiveSuccessors.getOrDefault(bb, List.of())) {
          if (successor == newIpdom || !idom.containsKey(successor)) {
            continue;
          }
          newIpdom = intersect(successor, newIpdom, idom, order);
        }

        if (idom.get(bb) != newIpdom) {
          idom.put(bb, newIpdom);
          changed = true;
        }
      }
    } while (changed);

    Map<BasicBlock, Object> ipdom = new LinkedHashMap<>();
    for (BasicBlock bb : reachableBlocks) {
      Object parent = idom.get(bb);
      if (parent != null && parent != bb) {
        ipdom.put(bb, parent);
      }
    }
    return ipdom;
  }

  private static Map<Object, List<Object>> buildReverseSuccessors(
      Set<BasicBlock> reachableBlocks, Map<BasicBlock, List<Object>> effectiveSuccessors) {
    Map<Object, List<Object>> reverseSuccessors = new IdentityHashMap<>();
    reverseSuccessors.put(VIRTUAL_EXIT, new ArrayList<>());
    for (BasicBlock bb : reachableBlocks) {
      reverseSuccessors.put(bb, new ArrayList<>());
    }
    for (BasicBlock bb : reachableBlocks) {
      for (Object succ : effectiveSuccessors.getOrDefault(bb, List.of())) {
        reverseSuccessors.computeIfAbsent(succ, ignored -> new ArrayList<>()).add(bb);
      }
    }
    return reverseSuccessors;
  }

  private static List<Object> computeReversePostOrder(Map<Object, List<Object>> reverseSuccessors) {
    List<Object> postOrder = new ArrayList<>();
    Set<Object> visited = Collections.newSetFromMap(new IdentityHashMap<>());
    computePostOrder(VIRTUAL_EXIT, reverseSuccessors, visited, postOrder);
    Collections.reverse(postOrder);
    return postOrder;
  }

  private static void computePostOrder(
      Object node,
      Map<Object, List<Object>> reverseSuccessors,
      Set<Object> visited,
      List<Object> postOrder) {
    if (!visited.add(node)) {
      return;
    }
    for (Object succ : reverseSuccessors.getOrDefault(node, List.of())) {
      computePostOrder(succ, reverseSuccessors, visited, postOrder);
    }
    postOrder.add(node);
  }

  private static Object intersect(
      Object first, Object second, Map<Object, Object> idom, Map<Object, Integer> order) {
    Object finger1 = first;
    Object finger2 = second;
    while (finger1 != finger2) {
      while (order.get(finger1) > order.get(finger2)) {
        finger1 = idom.get(finger1);
      }
      while (order.get(finger2) > order.get(finger1)) {
        finger2 = idom.get(finger2);
      }
    }
    return finger1;
  }

  private static void computePostDominanceFrontier(
      Set<BasicBlock> reachableBlocks,
      Map<BasicBlock, List<Object>> effectiveSuccessors,
      Map<BasicBlock, Object> ipdom,
      Map<BasicBlock, Set<BasicBlock>> frontier) {
    for (BasicBlock bb : reachableBlocks) {
      List<Object> succs = effectiveSuccessors.getOrDefault(bb, List.of());
      if (succs.size() < 2) continue;

      Object bbIpdom = ipdom.getOrDefault(bb, VIRTUAL_EXIT);
      for (Object succ : succs) {
        Object runner = succ;
        while (runner instanceof BasicBlock runnerBlock && runner != bbIpdom) {
          frontier.get(runnerBlock).add(bb);
          runner = ipdom.getOrDefault(runnerBlock, VIRTUAL_EXIT);
        }
      }
    }
  }
}
