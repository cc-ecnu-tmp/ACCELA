package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Simplifies the CFG: unreachable block removal, single-predecessor PHI folding, merging a block
 * into its unique predecessor when the predecessor has a single successor, and removal of empty
 * blocks that only forward control with an unconditional branch.
 */
public final class SimplifyCFG {
  private SimplifyCFG() {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (!runOnFunction(function)) {
        return PreservedAnalyses.all();
      }
      return PreservedAnalyses.none();
    }
  }

  /** Runs CFG simplification until a fixed point. @return whether the function changed */
  public static boolean runOnFunction(Function function) {
    boolean changed = false;
    while (true) {
      boolean iter =
          foldSingleEntryPhis(function)
              | removeUnreachableBlocks(function)
              | mergeBlockIntoPredecessor(function)
              | eliminateEmptyUnconditionalBranch(function);
      if (!iter) {
        break;
      }
      changed = true;
    }
    return changed;
  }

  private static boolean foldSingleEntryPhis(Function function) {
    boolean changed = false;
    for (BasicBlock bb : new ArrayList<>(function.getBlocks())) {
      List<BasicBlock> preds = bb.getPredecessors();
      if (preds.size() != 1) {
        continue;
      }
      BasicBlock pred = preds.get(0);
      while (!bb.getInstructions().isEmpty()) {
        Instruction inst = bb.getInstructions().get(0);
        if (inst.getOpcode() != Opcode.PHI) {
          break;
        }
        Value incoming = null;
        for (int i = 0; i < inst.getNumOperands(); i += 2) {
          if (inst.getOperand(i + 1) == pred) {
            incoming = inst.getOperand(i);
            break;
          }
        }
        if (incoming == null) {
          break;
        }
        inst.replaceAllUsesWith(incoming);
        inst.eraseFromParent();
        changed = true;
      }
    }
    return changed;
  }

  private static boolean removeUnreachableBlocks(Function function) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) {
      return false;
    }
    Set<BasicBlock> reachable = new HashSet<>();
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
    boolean changed = false;
    List<BasicBlock> dead = new ArrayList<>();
    for (BasicBlock bb : function.getBlocks()) {
      if (!reachable.contains(bb)) {
        dead.add(bb);
      }
    }
    for (BasicBlock bb : dead) {
      for (Instruction inst : new ArrayList<>(bb.getInstructions())) {
        inst.eraseFromParent();
      }
      function.removeBlock(bb);
      changed = true;
    }
    return changed;
  }

  /** Single distinct successor (e.g. cond br to the same target counts as one). */
  private static BasicBlock getUniqueSuccessor(BasicBlock bb) {
    List<BasicBlock> succs = bb.getSuccessors();
    if (succs.isEmpty()) {
      return null;
    }
    BasicBlock first = succs.get(0);
    for (BasicBlock s : succs) {
      if (s != first) {
        return null;
      }
    }
    return first;
  }

  private static boolean mergeBlockIntoPredecessor(Function function) {
    boolean changed = false;
    for (BasicBlock bb : new ArrayList<>(function.getBlocks())) {
      List<BasicBlock> preds = bb.getPredecessors();
      if (preds.size() != 1) {
        continue;
      }
      BasicBlock pred = preds.get(0);
      if (pred == bb) {
        continue;
      }
      if (getUniqueSuccessor(pred) != bb) {
        continue;
      }
      Instruction predTerm = pred.getTerminator();
      Instruction bbTerm = bb.getTerminator();
      if (predTerm == null || bbTerm == null) {
        continue;
      }
      List<Instruction> toMove = new ArrayList<>();
      for (Instruction inst : bb.getInstructions()) {
        if (inst == bbTerm) {
          break;
        }
        toMove.add(inst);
      }
      for (Instruction inst : toMove) {
        bb.remove(inst);
        pred.addInstruction(inst);
      }
      predTerm.eraseFromParent();
      bb.replaceAllUsesWith(pred);
      bb.remove(bbTerm);
      pred.addInstruction(bbTerm);
      function.removeBlock(bb);
      changed = true;
    }
    return changed;
  }

  /**
   * Removes blocks that contain only PHI nodes (optional) and a single unconditional branch,
   * threading incoming edges to the successor and rewriting PHIs.
   */
  private static boolean eliminateEmptyUnconditionalBranch(Function function) {
    boolean changed = false;
    for (BasicBlock bb : new ArrayList<>(function.getBlocks())) {
      Instruction term = bb.getTerminator();
      if (term == null || term.getOpcode() != Opcode.BR) {
        continue;
      }
      BasicBlock succ = (BasicBlock) term.getOperand(0);
      if (succ == bb) {
        continue;
      }
      boolean onlyPhiOrBr = true;
      for (Instruction inst : bb.getInstructions()) {
        if (inst == term) {
          break;
        }
        if (inst.getOpcode() != Opcode.PHI) {
          onlyPhiOrBr = false;
          break;
        }
      }
      if (!onlyPhiOrBr) {
        continue;
      }
      List<BasicBlock> predsOfBb = new ArrayList<>(bb.getPredecessors());
      if (predsOfBb.isEmpty()) {
        continue;
      }
      for (BasicBlock pred : predsOfBb) {
        Instruction pterm = pred.getTerminator();
        if (pterm == null) {
          continue;
        }
        for (int i = 0; i < pterm.getNumOperands(); i++) {
          if (pterm.getOperand(i) == bb) {
            pterm.setOperand(i, succ);
          }
        }
      }
      for (Instruction inst : new ArrayList<>(succ.getInstructions())) {
        if (inst.getOpcode() != Opcode.PHI) {
          break;
        }
        rewritePhiAfterRemovingEmptyBlock(inst, bb, predsOfBb);
      }
      term.eraseFromParent();
      function.removeBlock(bb);
      changed = true;
    }
    return changed;
  }

  private static void rewritePhiAfterRemovingEmptyBlock(
      Instruction phi, BasicBlock removed, List<BasicBlock> predsOfRemoved) {
    int n = phi.getNumOperands();
    int bbPairIndex = -1;
    Value valFromRemoved = null;
    for (int i = 0; i < n; i += 2) {
      if (phi.getOperand(i + 1) == removed) {
        bbPairIndex = i;
        valFromRemoved = phi.getOperand(i);
        break;
      }
    }
    if (bbPairIndex < 0) {
      return;
    }
    List<Value> newPairs = new ArrayList<>();
    for (int i = 0; i < n; i += 2) {
      if (i == bbPairIndex) {
        continue;
      }
      newPairs.add(phi.getOperand(i));
      newPairs.add(phi.getOperand(i + 1));
    }
    for (BasicBlock pred : predsOfRemoved) {
      Value v = mapValueLeavingBlock(removed, pred, valFromRemoved);
      newPairs.add(v);
      newPairs.add(pred);
    }
    phi.clearAllOperands();
    for (Value v : newPairs) {
      phi.addOperand(v);
    }
  }

  private static Value mapValueLeavingBlock(BasicBlock block, BasicBlock pred, Value v) {
    if (!(v instanceof Instruction inst)) {
      return v;
    }
    if (inst.getParent() != block) {
      return v;
    }
    if (inst.getOpcode() != Opcode.PHI) {
      throw new IllegalStateException("expected only PHIs in empty forwarding block");
    }
    for (int i = 0; i < inst.getNumOperands(); i += 2) {
      if (inst.getOperand(i + 1) == pred) {
        return mapValueLeavingBlock(block, pred, inst.getOperand(i));
      }
    }
    throw new IllegalStateException("PHI missing incoming value for predecessor");
  }
}
