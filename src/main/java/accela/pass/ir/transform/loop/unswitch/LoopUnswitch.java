package accela.pass.ir.transform.loop.unswitch;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.transform.CFGUpdateUtils;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Conservatively unswitches one invariant, loop-internal condition in an innermost loop. */
public final class LoopUnswitch {
  private static final int MAX_CLONED_INSTRUCTIONS = 24;
  private static int nextId;

  private LoopUnswitch() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops =
        fam.getResult(LoopAnalysis.class, function).loops();
    for (LoopAnalysis.Loop loop : loops) {
      if (hasSubloop(loop, loops)) continue;
      Candidate candidate = findCandidate(loop);
      if (candidate == null) continue;
      boolean safe = isSafe(
          function,
          candidate,
          fam.getResult(DominatorTreeAnalysis.class, function));
      if (!safe) continue;
      transform(function, candidate);
      return true;
    }
    return false;
  }

  private static Candidate findCandidate(LoopAnalysis.Loop loop) {
    if (loop.preheader() == null || loop.latches().size() != 1) return null;
    Instruction selected = null;
    for (BasicBlock block : loop.blocks()) {
      Instruction terminator = block.getTerminator();
      if (terminator == null || terminator.getOpcode() != Instruction.Opcode.CONDBR) continue;
      BasicBlock onTrue = (BasicBlock) terminator.getOperand(1);
      BasicBlock onFalse = (BasicBlock) terminator.getOperand(2);
      if (!loop.contains(onTrue) || !loop.contains(onFalse)) continue;
      Value condition = terminator.getOperand(0);
      if (condition instanceof Constant) continue;
      if (condition instanceof Instruction definition && loop.contains(definition.getParent())) {
        continue;
      }
      if (selected != null) return null;
      selected = terminator;
    }
    return selected == null ? null : new Candidate(loop, selected);
  }

  private static boolean isSafe(
      Function function,
      Candidate candidate,
      DominatorTreeAnalysis.Result dominators) {
    LoopAnalysis.Loop loop = candidate.loop();
    Instruction preheaderTerminator = loop.preheader().getTerminator();
    if (preheaderTerminator == null
        || preheaderTerminator.getOpcode() != Instruction.Opcode.BR
        || preheaderTerminator.getOperand(0) != loop.header()) return false;
    Value condition = candidate.branch().getOperand(0);
    if (condition instanceof Instruction definition
        && !dominators.dominates(definition.getParent(), loop.preheader())) return false;
    int instructions = 0;
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        instructions++;
        if (instruction.getOpcode() == Instruction.Opcode.CALL
            || instruction.getOpcode() == Instruction.Opcode.STORE
            || instruction.getOpcode() == Instruction.Opcode.ALLOCA
            || instruction.getOpcode() == Instruction.Opcode.RET) return false;
        for (Use use : instruction.getUses()) {
          if (loop.contains(use.getUser().getParent())) continue;
          if (!isExitPhiUse(use, loop)) return false;
        }
      }
    }
    if (instructions > MAX_CLONED_INSTRUCTIONS) return false;
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor)
            && successor.getPredecessors().stream().anyMatch(pred -> !loop.contains(pred))) {
          return false;
        }
      }
    }
    return function.getBlocks().containsAll(loop.blocks());
  }

  private static void transform(Function function, Candidate candidate) {
    int id = nextId++;
    LoopAnalysis.Loop loop = candidate.loop();
    List<BasicBlock> sources = function.getBlocks().stream().filter(loop::contains).toList();
    Map<BasicBlock, BasicBlock> blocks = new IdentityHashMap<>();
    BasicBlock insertion = sources.getLast();
    for (BasicBlock source : sources) {
      BasicBlock copy = function.insertBlockAfter(
          insertion, source.getLabel() + ".unswitch." + id);
      blocks.put(source, copy);
      insertion = copy;
    }

    Map<Value, Value> values = new IdentityHashMap<>();
    Map<Instruction, Instruction> instructions = new IdentityHashMap<>();
    for (BasicBlock sourceBlock : sources) {
      for (Instruction source : sourceBlock.getInstructions()) {
        Instruction copy = source.copyWithoutOperands();
        blocks.get(sourceBlock).addInstruction(copy);
        values.put(source, copy);
        instructions.put(source, copy);
      }
    }
    for (var entry : instructions.entrySet()) {
      Instruction source = entry.getKey();
      Instruction copy = entry.getValue();
      for (int index = 0; index < source.getNumOperands(); index++) {
        Value operand = source.getOperand(index);
        if (operand instanceof BasicBlock block) {
          copy.addOperand(blocks.getOrDefault(block, block));
        } else {
          copy.addOperand(values.getOrDefault(operand, operand));
        }
      }
    }

    extendExitPhis(loop, blocks, values);

    Value condition = candidate.branch().getOperand(0);
    BasicBlock originalBlock = candidate.branch().getParent();
    BasicBlock trueTarget = (BasicBlock) candidate.branch().getOperand(1);
    BasicBlock falseTarget = (BasicBlock) candidate.branch().getOperand(2);
    CFGUpdateUtils.rewriteCondBrToBr(originalBlock, trueTarget);
    Instruction clonedBranch = instructions.get(candidate.branch());
    BasicBlock clonedFalseTarget = blocks.get(falseTarget);
    CFGUpdateUtils.rewriteCondBrToBr(clonedBranch.getParent(), clonedFalseTarget);

    Instruction oldEntry = loop.preheader().getTerminator();
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(oldEntry);
    builder.createCondBr(
        condition, loop.header(), blocks.get(loop.header()));
    oldEntry.eraseFromParent();
  }

  private static void extendExitPhis(
      LoopAnalysis.Loop loop,
      Map<BasicBlock, BasicBlock> blocks,
      Map<Value, Value> values) {
    List<BasicBlock> exits = new ArrayList<>();
    for (BasicBlock block : loop.blocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (!loop.contains(successor) && !exits.contains(successor)) exits.add(successor);
      }
    }
    for (BasicBlock exit : exits) {
      for (Instruction phi : exit.getInstructions()) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        List<Value> additions = new ArrayList<>();
        for (int index = 0; index < phi.getNumOperands(); index += 2) {
          BasicBlock predecessor = (BasicBlock) phi.getOperand(index + 1);
          if (!loop.contains(predecessor)) continue;
          Value incoming = phi.getOperand(index);
          additions.add(values.getOrDefault(incoming, incoming));
          additions.add(blocks.get(predecessor));
        }
        for (Value addition : additions) phi.addOperand(addition);
      }
    }
  }

  private static boolean isExitPhiUse(Use use, LoopAnalysis.Loop loop) {
    Instruction user = use.getUser();
    int index = use.getOperandIndex();
    return user.getOpcode() == Instruction.Opcode.PHI
        && (index & 1) == 0
        && index + 1 < user.getNumOperands()
        && user.getOperand(index + 1) instanceof BasicBlock predecessor
        && loop.contains(predecessor)
        && !loop.contains(user.getParent());
  }

  private static boolean hasSubloop(
      LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> loops) {
    return loops.stream().anyMatch(other -> other != loop
        && loop.contains(other.header()) && other.blocks().size() < loop.blocks().size());
  }

  private record Candidate(LoopAnalysis.Loop loop, Instruction branch) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopUnswitch.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
