package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/** Canonicalizes natural loops; currently forms LLVM's unique-backedge guarantee. */
public final class LoopSimplify {
  private LoopSimplify() {}

  public static boolean runOnFunction(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
      if (loop.preheader() == null) {
        insertPreheader(function, loop);
        changed = true;
      }
      if (loop.latches().size() > 1) {
        insertUniqueBackedge(function, loop.header(), loop.latches());
        changed = true;
      }
    }
    return changed;
  }

  private static void insertPreheader(Function function, LoopAnalysis.Loop loop) {
    BasicBlock header = loop.header();
    List<BasicBlock> outside =
        function.getBlocks().stream()
            .filter(block -> !loop.contains(block) && block.getSuccessors().contains(header))
            .toList();
    if (outside.isEmpty()) throw new IllegalStateException("loop header has no entering edge");

    BasicBlock preheader =
        function.insertBlockAfter(
            outside.getLast(), header.getLabel() + ".preheader." + function.getBlocks().size());
    IRBuilder builder = new IRBuilder(preheader);
    Set<BasicBlock> outsideSet = Set.copyOf(outside);
    for (Instruction phi : new ArrayList<>(header.getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value entryValue = mergeIncomingValues(builder, phi, outside);
      List<Value> loopPairs = incomingPairsExcept(phi, outsideSet);
      phi.clearAllOperands();
      loopPairs.forEach(phi::addOperand);
      phi.addOperand(entryValue);
      phi.addOperand(preheader);
    }
    builder.createBr(header);
    for (BasicBlock predecessor : outside) {
      replaceSuccessor(predecessor, header, preheader);
    }
  }

  private static void insertUniqueBackedge(
      Function function, BasicBlock header, Set<BasicBlock> latchSet) {
    List<BasicBlock> latches =
        function.getBlocks().stream().filter(latchSet::contains).toList();
    BasicBlock backedge =
        function.insertBlockAfter(
            latches.getLast(), header.getLabel() + ".backedge." + function.getBlocks().size());
    IRBuilder builder = new IRBuilder(backedge);

    for (Instruction phi : new ArrayList<>(header.getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      Value backedgeValue = mergeBackedgeValues(builder, phi, latches);
      List<Value> outside = incomingPairsExcept(phi, latchSet);
      phi.clearAllOperands();
      outside.forEach(phi::addOperand);
      phi.addOperand(backedgeValue);
      phi.addOperand(backedge);
    }
    builder.createBr(header);
    for (BasicBlock latch : latches) replaceSuccessor(latch, header, backedge);
  }

  private static Value mergeBackedgeValues(
      IRBuilder builder, Instruction headerPhi, List<BasicBlock> latches) {
    Long step = commonConstantStep(headerPhi, latches);
    if (step != null && headerPhi.getType() == Type.INT) {
      return builder.createAdd(headerPhi, Constant.intConst(step));
    }
    return mergeIncomingValues(builder, headerPhi, latches);
  }

  private static Long commonConstantStep(
      Instruction headerPhi, List<BasicBlock> latches) {
    Long common = null;
    for (BasicBlock latch : latches) {
      Long step = constantStep(incomingValue(headerPhi, latch), headerPhi);
      if (step == null || common != null && !common.equals(step)) return null;
      common = step;
    }
    return common;
  }

  private static Long constantStep(Value value, Instruction phi) {
    if (!(value instanceof Instruction update)) return null;
    if (update.getOpcode() == Instruction.Opcode.ADD) {
      if (update.getOperand(0) == phi && update.getOperand(1) instanceof Constant.Int c) {
        return c.value;
      }
      if (update.getOperand(1) == phi && update.getOperand(0) instanceof Constant.Int c) {
        return c.value;
      }
    }
    if (update.getOpcode() == Instruction.Opcode.SUB
        && update.getOperand(0) == phi
        && update.getOperand(1) instanceof Constant.Int c) {
      return -c.value;
    }
    return null;
  }

  private static Value mergeIncomingValues(
      IRBuilder builder, Instruction headerPhi, List<BasicBlock> latches) {
    List<Value> values = new ArrayList<>();
    for (BasicBlock latch : latches) values.add(incomingValue(headerPhi, latch));
    if (values.stream().allMatch(value -> value == values.getFirst())) return values.getFirst();

    Instruction phi = builder.createPhi(headerPhi.getType());
    for (int index = 0; index < latches.size(); index++) {
      phi.addOperand(values.get(index));
      phi.addOperand(latches.get(index));
    }
    return phi;
  }

  private static List<Value> incomingPairsExcept(
      Instruction phi, Set<BasicBlock> excluded) {
    List<Value> result = new ArrayList<>();
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (excluded.contains((BasicBlock) phi.getOperand(index + 1))) continue;
      result.add(phi.getOperand(index));
      result.add(phi.getOperand(index + 1));
    }
    return result;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    throw new IllegalStateException("loop PHI has no latch value");
  }

  private static void replaceSuccessor(
      BasicBlock block, BasicBlock oldTarget, BasicBlock newTarget) {
    Instruction terminator = block.getTerminator();
    for (int index = 0; index < terminator.getNumOperands(); index++) {
      if (terminator.getOperand(index) == oldTarget) terminator.setOperand(index, newTarget);
    }
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
