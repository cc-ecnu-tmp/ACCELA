package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.PostDominatorTreeAnalysis;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Aggressive dead code elimination for the project IR.
 *
 * <p>The pass assumes instructions are dead until proven live, propagates data and control liveness
 * backward from side-effecting roots, rewrites dead conditional branches into unconditional ones,
 * and finally removes the remaining dead instructions from still-reachable blocks.
 */
public final class ADCE {
  private ADCE() {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      PostDominatorTreeAnalysis.Result postDomTree =
          fam.getResult(PostDominatorTreeAnalysis.class, function);
      if (!runOnFunction(function, postDomTree)) {
        return PreservedAnalyses.all();
      }
      return PreservedAnalyses.none();
    }
  }

  /** Removes module objects that cannot affect the SysY entry point. */
  public static final class GlobalPass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  public static boolean runOnModule(accela.ir.Module module) {
    return GlobalDCE.runOnModule(module);
  }

  public static boolean runOnFunction(Function function) {
    return runOnFunction(function, new PostDominatorTreeAnalysis().run(function, null));
  }

  public static boolean runOnFunction(
      Function function, PostDominatorTreeAnalysis.Result postDomTree) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    State state = new State(collectReachableBlocks(entry));
    initialize(function, state);
    markLiveInstructions(postDomTree, state);
    boolean changedControlFlow = rewriteDeadTerminators(postDomTree, state);

    Set<BasicBlock> finalReachableBlocks = collectReachableBlocks(entry);
    boolean removedUnreachableBlocks = removeNewlyUnreachableBlocks(function, finalReachableBlocks);
    List<Instruction> deadInstructions =
        collectDeadInstructions(finalReachableBlocks, state.liveInstructions);
    if (!changedControlFlow && !removedUnreachableBlocks && deadInstructions.isEmpty()) {
      return false;
    }

    for (Instruction inst : deadInstructions) {
      inst.dropAllReferences();
    }
    for (Instruction inst : deadInstructions) {
      inst.eraseFromParent();
    }
    return true;
  }

  private static boolean removeNewlyUnreachableBlocks(
      Function function, Set<BasicBlock> finalReachableBlocks) {
    List<BasicBlock> unreachableBlocks = new ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!finalReachableBlocks.contains(block)) {
        unreachableBlocks.add(block);
      }
    }
    if (unreachableBlocks.isEmpty()) {
      return false;
    }

    Set<BasicBlock> unreachableSet = new LinkedHashSet<>(unreachableBlocks);
    for (BasicBlock block : unreachableBlocks) {
      for (BasicBlock successor : new ArrayList<>(block.getSuccessors())) {
        if (!unreachableSet.contains(successor)) {
          CFGUpdateUtils.removePredecessorEdge(block, successor);
        }
      }
    }
    for (BasicBlock block : unreachableBlocks) {
      for (Instruction inst : new ArrayList<>(block.getInstructions())) {
        inst.dropAllReferences();
      }
      for (Instruction inst : new ArrayList<>(block.getInstructions())) {
        inst.eraseFromParent();
      }
      function.removeBlock(block);
    }
    return true;
  }

  private static void initialize(Function function, State state) {
    for (BasicBlock bb : state.reachableBlocks) {
      for (Instruction inst : bb.getInstructions()) {
        if (isAlwaysLiveRoot(inst)) {
          markLive(inst, state);
        }
      }
    }

    Instruction entryTerm = function.getEntryBlock().getTerminator();
    if (entryTerm != null && entryTerm.getOpcode() == Opcode.BR) {
      markLive(entryTerm, state);
    }
    markLoopBackEdgeTerminators(function.getEntryBlock(), state);

    state.blocksWithDeadTerminators.clear();
    for (BasicBlock bb : state.reachableBlocks) {
      Instruction term = bb.getTerminator();
      if (term != null && !state.liveInstructions.contains(term)) {
        state.blocksWithDeadTerminators.add(bb);
      }
    }
  }

  private static void markLiveInstructions(
      PostDominatorTreeAnalysis.Result postDomTree, State state) {
    do {
      while (!state.worklist.isEmpty()) {
        Instruction liveInst = state.worklist.removeLast();
        for (int i = 0; i < liveInst.getNumOperands(); i++) {
          Value operand = liveInst.getOperand(i);
          if (operand instanceof Instruction operandInst
              && operandInst.getParent() != null
              && state.reachableBlocks.contains(operandInst.getParent())) {
            markLive(operandInst, state);
          }
        }
        if (liveInst.getOpcode() == Opcode.PHI) {
          markPhiLive(liveInst.getParent(), state);
        }
      }
    } while (markLiveBranchesFromControlDependences(postDomTree, state));
  }

  private static List<Instruction> collectDeadInstructions(
      Set<BasicBlock> reachableBlocks, Set<Instruction> liveInstructions) {
    List<Instruction> deadInstructions = new ArrayList<>();
    for (BasicBlock bb : reachableBlocks) {
      List<Instruction> instructions = bb.getInstructions();
      for (int i = instructions.size() - 1; i >= 0; i--) {
        Instruction inst = instructions.get(i);
        if (!liveInstructions.contains(inst)) {
          deadInstructions.add(inst);
        }
      }
    }
    return deadInstructions;
  }

  private static boolean isAlwaysLiveRoot(Instruction inst) {
    return inst.getOpcode() == Opcode.RET
        || inst.getOpcode() == Opcode.STORE
        || inst.getOpcode() == Opcode.CALL;
  }

  private static void markLive(Instruction inst, State state) {
    if (!state.liveInstructions.add(inst)) {
      return;
    }
    state.worklist.addLast(inst);
    markBlockLive(inst.getParent(), state);
    if (inst.isTerminator()) {
      state.blocksWithDeadTerminators.remove(inst.getParent());
      for (BasicBlock succ : inst.getParent().getSuccessors()) {
        if (state.reachableBlocks.contains(succ)) {
          markBlockLive(succ, state);
        }
      }
    }
  }

  private static void markBlockLive(BasicBlock block, State state) {
    if (block == null || !state.liveBlocks.add(block)) {
      return;
    }
    state.newControlLiveBlocks.add(block);
    Instruction term = block.getTerminator();
    if (term != null && term.getOpcode() == Opcode.BR) {
      markLive(term, state);
    }
  }

  private static void markPhiLive(BasicBlock phiBlock, State state) {
    if (!state.blocksWithLivePhi.add(phiBlock)) {
      return;
    }
    for (BasicBlock pred : phiBlock.getPredecessors()) {
      if (state.reachableBlocks.contains(pred)) {
        state.newControlLiveBlocks.add(pred);
      }
    }
  }

  private static boolean markLiveBranchesFromControlDependences(
      PostDominatorTreeAnalysis.Result postDomTree, State state) {
    if (state.blocksWithDeadTerminators.isEmpty() || state.newControlLiveBlocks.isEmpty()) {
      return false;
    }

    boolean changed = false;
    List<BasicBlock> newLiveBlocks = new ArrayList<>(state.newControlLiveBlocks);
    state.newControlLiveBlocks.clear();
    for (BasicBlock liveBlock : newLiveBlocks) {
      for (BasicBlock controlSource : postDomTree.getPostDominanceFrontier(liveBlock)) {
        if (!state.blocksWithDeadTerminators.contains(controlSource)) {
          continue;
        }
        Instruction term = controlSource.getTerminator();
        if (term == null || term.getOpcode() != Opcode.CONDBR) {
          continue;
        }
        markLive(term, state);
        changed = true;
      }
    }
    return changed;
  }

  private static boolean rewriteDeadTerminators(
      PostDominatorTreeAnalysis.Result postDomTree, State state) {
    boolean changedControlFlow = false;
    for (BasicBlock block : new ArrayList<>(state.reachableBlocks)) {
      Instruction term = block.getTerminator();
      if (term == null || state.liveInstructions.contains(term)) {
        continue;
      }
      if (term.getOpcode() == Opcode.BR) {
        markLive(term, state);
        continue;
      }
      if (term.getOpcode() != Opcode.CONDBR) {
        continue;
      }

      BasicBlock target = choosePreferredSuccessor(block, postDomTree, state);
      CFGUpdateUtils.rewriteCondBrToBr(block, target);
      changedControlFlow = true;
      markLive(block.getTerminator(), state);
    }
    return changedControlFlow;
  }

  private static BasicBlock choosePreferredSuccessor(
      BasicBlock block, PostDominatorTreeAnalysis.Result postDomTree, State state) {
    List<BasicBlock> successors = block.getSuccessors();
    BasicBlock preferred = successors.get(0);
    int bestScore = scoreSuccessor(preferred, block, postDomTree, state);
    for (int i = 1; i < successors.size(); i++) {
      BasicBlock candidate = successors.get(i);
      int score = scoreSuccessor(candidate, block, postDomTree, state);
      if (score > bestScore) {
        preferred = candidate;
        bestScore = score;
      }
    }
    return preferred;
  }

  private static int scoreSuccessor(
      BasicBlock successor,
      BasicBlock source,
      PostDominatorTreeAnalysis.Result postDomTree,
      State state) {
    int score = 0;
    if (state.liveBlocks.contains(successor)) {
      score += 4;
    }
    if (postDomTree.reachesExit(successor)) {
      score += 2;
    }
    if (postDomTree.postDominates(successor, source)) {
      score += 1;
    }
    return score;
  }

  private static void markLoopBackEdgeTerminators(BasicBlock entry, State state) {
    Set<BasicBlock> visited = Collections.newSetFromMap(new IdentityHashMap<>());
    Set<BasicBlock> onStack = Collections.newSetFromMap(new IdentityHashMap<>());
    markLoopBackEdgeTerminators(entry, state, visited, onStack);
  }

  private static void markLoopBackEdgeTerminators(
      BasicBlock block,
      State state,
      Set<BasicBlock> visited,
      Set<BasicBlock> onStack) {
    if (!state.reachableBlocks.contains(block) || !visited.add(block)) {
      return;
    }
    onStack.add(block);
    for (BasicBlock succ : block.getSuccessors()) {
      if (!state.reachableBlocks.contains(succ)) {
        continue;
      }
      if (onStack.contains(succ)) {
        Instruction term = block.getTerminator();
        if (term != null) {
          markLive(term, state);
        }
      } else {
        markLoopBackEdgeTerminators(succ, state, visited, onStack);
      }
    }
    onStack.remove(block);
  }

  private static Set<BasicBlock> collectReachableBlocks(BasicBlock entry) {
    Set<BasicBlock> reachable = new LinkedHashSet<>();
    Deque<BasicBlock> work = new ArrayDeque<>();
    work.add(entry);
    while (!work.isEmpty()) {
      BasicBlock bb = work.removeFirst();
      if (!reachable.add(bb)) {
        continue;
      }
      for (BasicBlock succ : bb.getSuccessors()) {
        work.addLast(succ);
      }
    }
    return reachable;
  }

  private static final class State {
    private final Set<BasicBlock> reachableBlocks;
    private final Set<Instruction> liveInstructions =
        Collections.newSetFromMap(new IdentityHashMap<>());
    private final Deque<Instruction> worklist = new ArrayDeque<>();
    private final Set<BasicBlock> liveBlocks =
        Collections.newSetFromMap(new IdentityHashMap<>());
    private final Set<BasicBlock> newControlLiveBlocks =
        Collections.newSetFromMap(new IdentityHashMap<>());
    private final Set<BasicBlock> blocksWithDeadTerminators =
        Collections.newSetFromMap(new IdentityHashMap<>());
    private final Set<BasicBlock> blocksWithLivePhi =
        Collections.newSetFromMap(new IdentityHashMap<>());

    private State(Set<BasicBlock> reachableBlocks) {
      this.reachableBlocks = reachableBlocks;
    }
  }
}
