package accela.backend.regalloc;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineOperand;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Marks machine blocks that belong to a cyclic strongly connected component. */
final class MachineLoopAnalysis {
  private MachineLoopAnalysis() {}

  static Set<MachineBasicBlock> findLoopBlocks(MachineFunction function) {
    State state = new State();
    for (MachineBasicBlock block : function.getBlocks()) {
      if (!state.index.containsKey(block)) visit(block, state);
    }
    return state.loopBlocks;
  }

  private static void visit(MachineBasicBlock block, State state) {
    int index = state.nextIndex++;
    state.index.put(block, index);
    state.lowlink.put(block, index);
    state.stack.push(block);
    state.onStack.add(block);
    for (MachineBasicBlock successor : successors(block)) {
      if (!state.index.containsKey(successor)) {
        visit(successor, state);
        state.lowlink.put(block, Math.min(
            state.lowlink.get(block), state.lowlink.get(successor)));
      } else if (state.onStack.contains(successor)) {
        state.lowlink.put(block, Math.min(
            state.lowlink.get(block), state.index.get(successor)));
      }
    }
    if (!state.lowlink.get(block).equals(state.index.get(block))) return;
    List<MachineBasicBlock> component = new ArrayList<>();
    MachineBasicBlock member;
    do {
      member = state.stack.pop();
      state.onStack.remove(member);
      component.add(member);
    } while (member != block);
    if (component.size() > 1 || successors(block).contains(block)) {
      state.loopBlocks.addAll(component);
    }
  }

  private static List<MachineBasicBlock> successors(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return List.of();
    List<MachineBasicBlock> successors = new ArrayList<>();
    for (MachineOperand operand :
        block.getInstructions().get(block.getInstructions().size() - 1).getOperands()) {
      if (operand instanceof BlockOperand target) successors.add(target.getBlock());
    }
    return successors;
  }

  private static final class State {
    private int nextIndex;
    private final Map<MachineBasicBlock, Integer> index = new IdentityHashMap<>();
    private final Map<MachineBasicBlock, Integer> lowlink = new IdentityHashMap<>();
    private final Deque<MachineBasicBlock> stack = new ArrayDeque<>();
    private final Set<MachineBasicBlock> onStack =
        Collections.newSetFromMap(new IdentityHashMap<>());
    private final Set<MachineBasicBlock> loopBlocks =
        Collections.newSetFromMap(new IdentityHashMap<>());
  }
}
