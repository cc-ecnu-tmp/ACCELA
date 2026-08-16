package accela.pass.ir.transform.simplifycfg;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.transform.CFGUpdateUtils;
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
          ShortCircuitBranchThreading.run(function)
              | TailMerge.run(function)
              | foldBooleanPhis(function)
              | foldSingleEntryPhis(function)
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

  /** Replaces a two-way 0/1 PHI diamond with the branch condition or its inverse. */
  private static boolean foldBooleanPhis(Function function) {
    boolean changed = false;
    for (BasicBlock merge : new ArrayList<>(function.getBlocks())) {
      for (Instruction phi : new ArrayList<>(merge.getInstructions())) {
        if (phi.getOpcode() != Opcode.PHI) break;
        Value replacement = booleanPhiValue(phi, merge);
        if (replacement == null) continue;
        phi.replaceAllUsesWith(replacement);
        phi.eraseFromParent();
        changed = true;
      }
    }
    return changed;
  }

  private static Value booleanPhiValue(Instruction phi, BasicBlock merge) {
    BooleanDiamond diamond = matchBooleanDiamond(phi, merge);
    if (diamond == null) return null;

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(diamond.branch());
    Value condition = diamond.branch().getOperand(0);
    if (diamond.negated()) {
      condition = builder.createXor(condition, Constant.boolConst(true));
    }
    return builder.createZExt(condition, Type.INT);
  }

  private static BooleanDiamond matchBooleanDiamond(Instruction phi, BasicBlock merge) {
    if (phi.getType() != Type.INT || phi.getNumOperands() != 4) return null;
    List<BasicBlock> predecessors = merge.getPredecessors();
    if (predecessors.size() != 2) return null;

    for (BasicBlock direct : predecessors) {
      Instruction branch = direct.getTerminator();
      if (branch == null || branch.getOpcode() != Opcode.CONDBR) continue;
      boolean mergeOnTrue = branch.getOperand(1) == merge;
      if (!mergeOnTrue && branch.getOperand(2) != merge) continue;
      BasicBlock forwarder = (BasicBlock) branch.getOperand(mergeOnTrue ? 2 : 1);
      if (!isEmptyForwarderFrom(forwarder, direct, merge)) continue;

      Boolean directValue = incomingBoolean(phi, direct);
      Boolean forwardedValue = incomingBoolean(phi, forwarder);
      if (directValue == null || forwardedValue == null) continue;
      if (directValue.equals(forwardedValue)) continue;
      boolean trueValue = mergeOnTrue ? directValue : forwardedValue;
      return new BooleanDiamond(branch, !trueValue);
    }
    return null;
  }

  private static boolean isEmptyForwarderFrom(
      BasicBlock block, BasicBlock predecessor, BasicBlock target) {
    Instruction term = block.getTerminator();
    return block.getInstructions().size() == 1
        && term != null
        && term.getOpcode() == Opcode.BR
        && term.getOperand(0) == target
        && block.getPredecessors().equals(List.of(predecessor));
  }

  private static Boolean incomingBoolean(Instruction phi, BasicBlock predecessor) {
    int index = findIncomingPairIndex(phi, predecessor);
    if (index < 0 || !(phi.getOperand(index) instanceof Constant.Int value)) return null;
    if (value.value == 0) return false;
    return value.value == 1 ? true : null;
  }

  private record BooleanDiamond(Instruction branch, boolean negated) {}

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
      for (BasicBlock successor : new ArrayList<>(bb.getSuccessors())) {
        if (reachable.contains(successor)) {
          CFGUpdateUtils.removePredecessorEdge(bb, successor);
        }
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
      if (phisEscapeSuccessorRewrite(bb, succ)) {
        continue;
      }
      if (!canThreadEmptyBlockThroughSuccessor(bb, succ, predsOfBb)) {
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
      for (Instruction inst : new ArrayList<>(bb.getInstructions())) {
        inst.eraseFromParent();
      }
      function.removeBlock(bb);
      changed = true;
    }
    return changed;
  }

  private static boolean phisEscapeSuccessorRewrite(BasicBlock block, BasicBlock succ) {
    for (Instruction inst : block.getInstructions()) {
      if (inst.getOpcode() == Opcode.BR) {
        break;
      }
      if (inst.getOpcode() != Opcode.PHI) {
        continue;
      }
      for (var use : new ArrayList<>(inst.getUses())) {
        Instruction user = use.getUser();
        if (user.getParent() != succ || user.getOpcode() != Opcode.PHI) {
          return true;
        }
      }
    }
    return false;
  }

  private static boolean canThreadEmptyBlockThroughSuccessor(
      BasicBlock removed, BasicBlock succ, List<BasicBlock> predsOfRemoved) {
    for (Instruction inst : succ.getInstructions()) {
      if (inst.getOpcode() != Opcode.PHI) {
        break;
      }
      int removedPairIndex = findIncomingPairIndex(inst, removed);
      if (removedPairIndex < 0) {
        continue;
      }
      Value valFromRemoved = inst.getOperand(removedPairIndex);
      for (BasicBlock pred : predsOfRemoved) {
        Value mapped = mapValueLeavingBlock(removed, pred, valFromRemoved);
        int existingPairIndex = findIncomingPairIndex(inst, pred);
        if (existingPairIndex >= 0
            && !sameIncomingValue(inst.getOperand(existingPairIndex), mapped)) {
          return false;
        }
      }
    }
    return true;
  }

  private static void rewritePhiAfterRemovingEmptyBlock(
      Instruction phi, BasicBlock removed, List<BasicBlock> predsOfRemoved) {
    int n = phi.getNumOperands();
    int bbPairIndex = findIncomingPairIndex(phi, removed);
    if (bbPairIndex < 0) {
      return;
    }
    Value valFromRemoved = phi.getOperand(bbPairIndex);
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
      appendIncomingIfAbsentOrEquivalent(newPairs, v, pred);
    }
    phi.clearAllOperands();
    for (Value v : newPairs) {
      phi.addOperand(v);
    }
  }

  private static int findIncomingPairIndex(Instruction phi, BasicBlock pred) {
    for (int i = 0; i < phi.getNumOperands(); i += 2) {
      if (phi.getOperand(i + 1) == pred) {
        return i;
      }
    }
    return -1;
  }

  private static void appendIncomingIfAbsentOrEquivalent(
      List<Value> operandPairs, Value value, BasicBlock pred) {
    for (int i = 0; i < operandPairs.size(); i += 2) {
      if (operandPairs.get(i + 1) == pred) {
        if (!sameIncomingValue(operandPairs.get(i), value)) {
          throw new IllegalStateException("cannot merge distinct incoming values for one predecessor");
        }
        return;
      }
    }
    operandPairs.add(value);
    operandPairs.add(pred);
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

  private static boolean sameIncomingValue(Value first, Value second) {
    if (first == second) {
      return true;
    }
    if (first instanceof Constant && second instanceof Constant) {
      return first.getType() == second.getType() && String.valueOf(first.getName()).equals(second.getName());
    }
    return false;
  }
}
