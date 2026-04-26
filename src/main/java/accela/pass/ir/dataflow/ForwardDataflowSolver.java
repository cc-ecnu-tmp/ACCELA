package accela.pass.ir.dataflow;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class ForwardDataflowSolver<T> {

  public static final class Edge {
    public final BasicBlock from;
    public final BasicBlock to;

    public Edge(BasicBlock from, BasicBlock to) {
      this.from = from;
      this.to = to;
    }

    @Override
    public int hashCode() {
      return System.identityHashCode(from) * 31 + System.identityHashCode(to);
    }

    @Override
    public boolean equals(Object o) {
      if (!(o instanceof Edge e)) return false;
      return from == e.from && to == e.to;
    }
  }

  public static final class Result<T> {
    public final Map<BasicBlock, T> blockFacts;
    public final Set<BasicBlock> reachableBlocks;
    public final Set<Edge> executableEdges;

    Result(Map<BasicBlock, T> blockFacts, Set<BasicBlock> reachableBlocks,
           Set<Edge> executableEdges) {
      this.blockFacts = blockFacts;
      this.reachableBlocks = reachableBlocks;
      this.executableEdges = executableEdges;
    }
  }

  public Result<T> solve(
      Function fn, Lattice<T> lattice, ForwardTransfer<T> transfer, T entryFact) {
    return solve(fn, lattice, transfer, entryFact, new LinkedHashSet<>());
  }

  public Result<T> solve(
      Function fn, Lattice<T> lattice, ForwardTransfer<T> transfer, T entryFact,
      Set<Edge> sharedEdges) {

    List<BasicBlock> blocks = fn.getBlocks();
    if (blocks.isEmpty()) return new Result<>(Map.of(), Set.of(), Set.of());

    Map<BasicBlock, T> blockFacts = new IdentityHashMap<>();
    Set<BasicBlock> reachable = Collections.newSetFromMap(new IdentityHashMap<>());
    Set<Edge> executableEdges = sharedEdges;
    T bot = lattice.bot();
    for (BasicBlock bb : blocks) {
      blockFacts.put(bb, bot);
    }
    blockFacts.put(fn.getEntryBlock(), entryFact);
    reachable.add(fn.getEntryBlock());

    List<BasicBlock> rpo = reversePostOrder(fn.getEntryBlock());
    Set<BasicBlock> inWorklist = Collections.newSetFromMap(new IdentityHashMap<>());
    Deque<BasicBlock> worklist = new ArrayDeque<>(rpo);
    inWorklist.addAll(rpo);

    while (!worklist.isEmpty()) {
      BasicBlock bb = worklist.poll();
      inWorklist.remove(bb);

      if (!reachable.contains(bb)) continue;

      T fact = blockFacts.get(bb);

      List<Instruction> instructions = bb.getInstructions();
      for (Instruction inst : instructions) {
        if (inst.isTerminator()) {
          Map<BasicBlock, T> outFacts = transfer.transferTerminator(inst, fact);
          for (var entry : outFacts.entrySet()) {
            BasicBlock succ = entry.getKey();
            T newFact = entry.getValue();
            T oldFact = blockFacts.get(succ);
            if (oldFact == null) continue;
            reachable.add(succ);
            executableEdges.add(new Edge(bb, succ));
            T joined = lattice.join(oldFact, newFact);
            if (!lattice.isEqual(joined, oldFact)) {
              blockFacts.put(succ, joined);
              if (inWorklist.add(succ)) {
                worklist.add(succ);
              }
            }
          }
        } else {
          fact = transfer.transferInstruction(inst, fact);
        }
      }
    }
    return new Result<>(blockFacts, reachable, executableEdges);
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
}
