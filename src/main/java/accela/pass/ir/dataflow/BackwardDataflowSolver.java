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

public final class BackwardDataflowSolver<T> {

  public Map<BasicBlock, T> solve(
      Function fn, Lattice<T> lattice, BackwardTransfer<T> transfer, T exitFact) {

    List<BasicBlock> blocks = fn.getBlocks();
    if (blocks.isEmpty()) return Map.of();

    Map<BasicBlock, T> blockExitFacts = new IdentityHashMap<>();
    T bot = lattice.bot();
    for (BasicBlock bb : blocks) {
      blockExitFacts.put(bb, bot);
    }

    for (BasicBlock bb : blocks) {
      Instruction term = bb.getTerminator();
      if (term != null && term.getOpcode() == Instruction.Opcode.RET) {
        blockExitFacts.put(bb, exitFact);
      }
    }

    List<BasicBlock> po = postOrder(fn.getEntryBlock());
    Set<BasicBlock> inWorklist = Collections.newSetFromMap(new IdentityHashMap<>());
    Deque<BasicBlock> worklist = new ArrayDeque<>(po);
    inWorklist.addAll(po);

    while (!worklist.isEmpty()) {
      BasicBlock bb = worklist.poll();
      inWorklist.remove(bb);

      T fact = blockExitFacts.get(bb);

      List<Instruction> instructions = bb.getInstructions();
      for (int i = instructions.size() - 1; i >= 0; i--) {
        fact = transfer.transferInstruction(instructions.get(i), fact);
      }

      for (BasicBlock pred : bb.getPredecessors()) {
        T oldFact = blockExitFacts.get(pred);
        if (oldFact == null) continue;
        T joined = lattice.join(oldFact, fact);
        if (!lattice.isEqual(joined, oldFact)) {
          blockExitFacts.put(pred, joined);
          if (inWorklist.add(pred)) {
            worklist.add(pred);
          }
        }
      }
    }
    return blockExitFacts;
  }

  private static List<BasicBlock> postOrder(BasicBlock entry) {
    List<BasicBlock> result = new ArrayList<>();
    Set<BasicBlock> visited = new LinkedHashSet<>();
    buildPostOrder(entry, visited, result);
    return result;
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
