package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Implementation of mem2reg's SSA-promotion algorithm.
 */
public final class PromoteMemoryToRegister {
  private PromoteMemoryToRegister() {}

  /** Promotes all eligible allocas in one function, returning whether anything changed. */
  public static boolean run(Function function, DominatorTreeAnalysis.Result domTree) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    List<Instruction> allocas = new ArrayList<>();
    for (Instruction inst : entry.getInstructions()) {
      if (isPromotableAlloca(inst)) {
        allocas.add(inst);
      }
    }

    if (allocas.isEmpty()) return false;

    for (Instruction alloca : allocas) {
      if (promoteSingleBlockAlloca(alloca)) continue;
      promoteMultiBlockAlloca(function, alloca, domTree);
    }
    return true;
  }

  /**
   * Promotes one temporary stack slot introduced by another transform.
   *
   * <p>The caller must preserve the CFG, so the supplied dominator tree remains valid.
   */
  public static void promoteAlloca(
      Function function, Instruction alloca, DominatorTreeAnalysis.Result domTree) {
    if (!isPromotableAlloca(alloca)) {
      throw new IllegalArgumentException("alloca is not promotable");
    }
    if (!promoteSingleBlockAlloca(alloca)) {
      promoteMultiBlockAlloca(function, alloca, domTree);
    }
  }

  /** Returns whether an alloca is simple enough for this pass to promote. */
  private static boolean isPromotableAlloca(Instruction inst) {
    if (inst.getOpcode() != Instruction.Opcode.ALLOCA) return false;

    Type allocType = inst.getAllocatedType();
    if (allocType == null) return false;
    if (allocType.isArray()) return false;

    for (Use use : inst.getUses()) {
      Instruction user = use.getUser();
      if (user.getOpcode() == Instruction.Opcode.LOAD) continue;
      if (user.getOpcode() == Instruction.Opcode.STORE && user.getOperand(1) == inst) continue;
      return false;
    }
    return true;
  }

  /** Fast path for allocas whose loads and stores are confined to a single block. */
  private static boolean promoteSingleBlockAlloca(Instruction alloca) {
    BasicBlock onlyBlock = null;
    for (Use use : alloca.getUses()) {
      Instruction user = use.getUser();
      BasicBlock bb = user.getParent();
      if (onlyBlock == null) {
        onlyBlock = bb;
      } else if (onlyBlock != bb) {
        return false;
      }
    }

    if (onlyBlock == null) {
      alloca.eraseFromParent();
      return true;
    }

    Value currentValue = null;
    List<Instruction> toRemove = new ArrayList<>();

    for (Instruction inst : new ArrayList<>(onlyBlock.getInstructions())) {
      if (inst.getOpcode() == Instruction.Opcode.STORE && inst.getOperand(1) == alloca) {
        currentValue = inst.getOperand(0);
        toRemove.add(inst);
      } else if (inst.getOpcode() == Instruction.Opcode.LOAD && inst.getOperand(0) == alloca) {
        if (currentValue == null) return false;
        inst.replaceAllUsesWith(currentValue);
        toRemove.add(inst);
      }
    }

    for (Instruction inst : toRemove) {
      inst.eraseFromParent();
    }
    alloca.eraseFromParent();
    return true;
  }

  /** Full SSA promotion path for allocas used across multiple basic blocks. */
  private static void promoteMultiBlockAlloca(
      Function function, Instruction alloca, DominatorTreeAnalysis.Result domTree) {
    Set<BasicBlock> defBlocks = collectDefBlocks(alloca);
    Map<BasicBlock, Instruction> phiByBlock = insertPhiForAlloca(alloca, defBlocks, domTree);
    renameValues(function, alloca, phiByBlock, domTree);
  }

  /** Collects the basic blocks that define the promoted stack slot. */
  private static Set<BasicBlock> collectDefBlocks(Instruction alloca) {
    Set<BasicBlock> defBlocks = new LinkedHashSet<>();
    for (Use use : alloca.getUses()) {
      Instruction user = use.getUser();
      if (user.getOpcode() == Instruction.Opcode.STORE && user.getOperand(1) == alloca) {
        defBlocks.add(user.getParent());
      }
    }
    return defBlocks;
  }

  private static Map<BasicBlock, Instruction> insertPhiForAlloca(
      Instruction alloca,
      Set<BasicBlock> defBlocks,
      DominatorTreeAnalysis.Result domTree) {
    Map<BasicBlock, Instruction> phiByBlock = new LinkedHashMap<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>(defBlocks);
    Set<BasicBlock> visited = new LinkedHashSet<>(defBlocks);

    while (!worklist.isEmpty()) {
      BasicBlock bb = worklist.removeFirst();
      for (BasicBlock frontier : domTree.getDominanceFrontier(bb)) {
        if (phiByBlock.containsKey(frontier)) continue;

        Instruction phi = Instruction.createPhi(alloca.getAllocatedType());
        frontier.addInstructionToFront(phi);
        phiByBlock.put(frontier, phi);

        if (visited.add(frontier)) {
          worklist.addLast(frontier);
        }
      }
    }

    return phiByBlock;
  }

  private static void renameValues(
      Function function,
      Instruction alloca,
      Map<BasicBlock, Instruction> phiByBlock,
      DominatorTreeAnalysis.Result domTree) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return;

    Deque<Value> valueStack = new ArrayDeque<>();
    renameBlock(entry, alloca, phiByBlock, domTree, valueStack);
    fillMissingPhiOperands(alloca, phiByBlock);
    alloca.eraseFromParent();
  }

  private static void renameBlock(
      BasicBlock bb,
      Instruction alloca,
      Map<BasicBlock, Instruction> phiByBlock,
      DominatorTreeAnalysis.Result domTree,
      Deque<Value> valueStack) {
    int pushedCount = 0;
    Instruction phi = phiByBlock.get(bb);
    if (phi != null) {
      valueStack.push(phi);
      pushedCount++;
    }

    List<Instruction> toRemove = new ArrayList<>();
    for (Instruction inst : new ArrayList<>(bb.getInstructions())) {
      if (inst == phi) continue;

      if (inst.getOpcode() == Instruction.Opcode.LOAD && inst.getOperand(0) == alloca) {
        Value replacement =
            valueStack.isEmpty() ? Constant.zero(alloca.getAllocatedType()) : valueStack.peek();
        inst.replaceAllUsesWith(replacement);
        toRemove.add(inst);
      } else if (inst.getOpcode() == Instruction.Opcode.STORE && inst.getOperand(1) == alloca) {
        valueStack.push(inst.getOperand(0));
        pushedCount++;
        toRemove.add(inst);
      }
    }

    for (BasicBlock succ : bb.getSuccessors()) {
      Instruction succPhi = phiByBlock.get(succ);
      if (succPhi == null) continue;
      Value incoming =
          valueStack.isEmpty() ? Constant.zero(alloca.getAllocatedType()) : valueStack.peek();
      succPhi.addOperand(incoming);
      succPhi.addOperand(bb);
    }

    for (Instruction inst : toRemove) {
      inst.eraseFromParent();
    }

    for (BasicBlock child : domTree.getChildren(bb)) {
      renameBlock(child, alloca, phiByBlock, domTree, valueStack);
    }

    for (int i = 0; i < pushedCount; i++) {
      valueStack.pop();
    }
  }

  /**
   * Adds default incoming values for predecessors that are unreachable from the entry block.
   *
   * <p>The renaming walk only visits blocks in the dominator tree rooted at the function entry.
   * If a block with a PHI also has an unreachable predecessor, LLVM IR still requires that edge to
   * appear in the PHI operand list even though the value is semantically irrelevant.
   */
  private static void fillMissingPhiOperands(
      Instruction alloca, Map<BasicBlock, Instruction> phiByBlock) {
    for (Map.Entry<BasicBlock, Instruction> entry : phiByBlock.entrySet()) {
      BasicBlock block = entry.getKey();
      Instruction phi = entry.getValue();
      Set<BasicBlock> incomingBlocks = new LinkedHashSet<>();
      for (int i = 1; i < phi.getNumOperands(); i += 2) {
        incomingBlocks.add((BasicBlock) phi.getOperand(i));
      }
      for (BasicBlock pred : collectPrintedPredecessors(block)) {
        if (incomingBlocks.add(pred)) {
          phi.addOperand(Constant.zero(alloca.getAllocatedType()));
          phi.addOperand(pred);
        }
      }
    }
  }

  private static List<BasicBlock> collectPrintedPredecessors(BasicBlock block) {
    List<BasicBlock> preds = new ArrayList<>();
    Function function = block.getParent();
    if (function == null) return preds;
    for (BasicBlock candidate : function.getBlocks()) {
      for (BasicBlock succ : candidate.getSuccessors()) {
        if (succ == block || succ.getLabel().equals(block.getLabel())) {
          preds.add(candidate);
          break;
        }
      }
    }
    return preds;
  }
}
