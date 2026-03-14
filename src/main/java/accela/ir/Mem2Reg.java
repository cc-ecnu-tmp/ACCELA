package accela.ir;

import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

public class Mem2Reg {
  public static void run(Module module) {
    for (Function function : module.getFunctions()) {
      runOnFunction(function);
    }
  }

  private static void runOnFunction(Function function) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return;

    List<Instruction> allocas = new ArrayList<>();
    for (Instruction inst : entry.getInstructions()) {
      if (isPromotableAlloca(inst)) {
        allocas.add(inst);
      }
    }

    for (Instruction alloca : allocas) {
      if (promoteSingleBlockAlloca(alloca)) continue;
      promoteMultiBlockAlloca(function, alloca);
    }
  }

  private static boolean isPromotableAlloca(Instruction inst) {
    if (inst.getOpcode() != Instruction.Opcode.ALLOCA) return false;

    Type allocType = inst.getAllocatedType();
    if (allocType == null) return false;
    if (allocType.isArray() || allocType.isPointer()) return false;

    for (Use use : inst.getUses()) {
      Instruction user = use.getUser();
      if (user.getOpcode() == Instruction.Opcode.LOAD) continue;
      if (user.getOpcode() == Instruction.Opcode.STORE && user.getOperand(1) == inst) continue;
      return false;
    }
    return true;
  }

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

  private static void promoteMultiBlockAlloca(Function function, Instruction alloca) {
    Set<BasicBlock> defBlocks = collectDefBlocks(alloca);
    DominatorInfo domInfo = computeDominators(function);
    Map<BasicBlock, Instruction> phiByBlock = insertPhiForAlloca(alloca, defBlocks, domInfo);
    renameValues(function, alloca, phiByBlock, domInfo);
  }

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

  private static DominatorInfo computeDominators(Function function) {
    DominatorInfo info = new DominatorInfo();
    List<BasicBlock> blocks = function.getBlocks();
    BasicBlock entry = function.getEntryBlock();
    if (entry == null || blocks.isEmpty()) return info;

    for (BasicBlock bb : blocks) {
      Set<BasicBlock> doms = new LinkedHashSet<>();
      if (bb == entry) {
        doms.add(entry);
      } else {
        doms.addAll(blocks);
      }
      info.dominators.put(bb, doms);
      info.domTreeChildren.put(bb, new ArrayList<>());
    }

    boolean changed;
    do {
      changed = false;
      for (BasicBlock bb : blocks) {
        if (bb == entry) continue;
        List<BasicBlock> preds = bb.getPredecessors();
        if (preds.isEmpty()) continue;

        Set<BasicBlock> newDoms = new LinkedHashSet<>(info.dominators.get(preds.get(0)));
        for (int i = 1; i < preds.size(); i++) {
          newDoms.retainAll(info.dominators.get(preds.get(i)));
        }
        newDoms.add(bb);

        if (!newDoms.equals(info.dominators.get(bb))) {
          info.dominators.put(bb, newDoms);
          changed = true;
        }
      }
    } while (changed);

    for (BasicBlock bb : blocks) {
      if (bb == entry) continue;
      BasicBlock idom = null;
      for (BasicBlock candidate : info.dominators.get(bb)) {
        if (candidate == bb) continue;
        boolean isImmediate = true;
        for (BasicBlock other : info.dominators.get(bb)) {
          if (other == bb || other == candidate) continue;
          if (info.dominators.get(other).contains(candidate)) {
            isImmediate = false;
            break;
          }
        }
        if (isImmediate) {
          idom = candidate;
          break;
        }
      }
      info.idom.put(bb, idom);
      if (idom != null) info.domTreeChildren.get(idom).add(bb);
    }

    for (BasicBlock bb : blocks) {
      Set<BasicBlock> frontier = new LinkedHashSet<>();
      for (BasicBlock succ : bb.getSuccessors()) {
        if (info.idom.get(succ) != bb) frontier.add(succ);
      }
      info.domFrontier.put(bb, frontier);
    }

    boolean dfChanged;
    do {
      dfChanged = false;
      for (BasicBlock bb : blocks) {
        Set<BasicBlock> frontier = new LinkedHashSet<>(info.domFrontier.get(bb));
        for (BasicBlock child : info.domTreeChildren.get(bb)) {
          for (BasicBlock frontierBlock : info.domFrontier.get(child)) {
            if (info.idom.get(frontierBlock) != bb) {
              frontier.add(frontierBlock);
            }
          }
        }
        if (!frontier.equals(info.domFrontier.get(bb))) {
          info.domFrontier.put(bb, frontier);
          dfChanged = true;
        }
      }
    } while (dfChanged);

    return info;
  }

  private static Map<BasicBlock, Instruction> insertPhiForAlloca(
      Instruction alloca, Set<BasicBlock> defBlocks, DominatorInfo domInfo) {
    Map<BasicBlock, Instruction> phiByBlock = new LinkedHashMap<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>(defBlocks);
    Set<BasicBlock> visited = new LinkedHashSet<>(defBlocks);

    while (!worklist.isEmpty()) {
      BasicBlock bb = worklist.removeFirst();
      for (BasicBlock frontier : domInfo.domFrontier.getOrDefault(bb, Set.of())) {
        if (phiByBlock.containsKey(frontier)) continue;

        Instruction phi = new Instruction(Instruction.Opcode.PHI, alloca.getAllocatedType());
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
      DominatorInfo domInfo) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return;

    Deque<Value> valueStack = new ArrayDeque<>();
    renameBlock(entry, alloca, phiByBlock, domInfo, valueStack);
    alloca.eraseFromParent();
  }

  private static void renameBlock(
      BasicBlock bb,
      Instruction alloca,
      Map<BasicBlock, Instruction> phiByBlock,
      DominatorInfo domInfo,
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
        Value replacement = valueStack.isEmpty()
            ? Constant.zero(alloca.getAllocatedType())
            : valueStack.peek();
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
      Value incoming = valueStack.isEmpty()
          ? Constant.zero(alloca.getAllocatedType())
          : valueStack.peek();
      succPhi.addOperand(incoming);
      succPhi.addOperand(bb);
    }

    for (Instruction inst : toRemove) {
      inst.eraseFromParent();
    }

    for (BasicBlock child : domInfo.domTreeChildren.getOrDefault(bb, List.of())) {
      renameBlock(child, alloca, phiByBlock, domInfo, valueStack);
    }

    for (int i = 0; i < pushedCount; i++) {
      valueStack.pop();
    }
  }

  private static class DominatorInfo {
    final Map<BasicBlock, Set<BasicBlock>> dominators = new LinkedHashMap<>();
    final Map<BasicBlock, BasicBlock> idom = new LinkedHashMap<>();
    final Map<BasicBlock, List<BasicBlock>> domTreeChildren = new LinkedHashMap<>();
    final Map<BasicBlock, Set<BasicBlock>> domFrontier = new LinkedHashMap<>();
  }
}
