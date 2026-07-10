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
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.IdentityHashMap;
import java.util.Map;

/** Replaces a small number of affine memory addresses with pointer recurrences. */
public final class LoopAddressStrengthReduction {
  private LoopAddressStrengthReduction() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    Map<LoopAnalysis.Loop, Integer> transformed = new IdentityHashMap<>();
    boolean changed = false;
    var inductions = fam.getResult(InductionVariableAnalysis.class, function).inductions();
    for (var induction : inductions) {
      LoopAnalysis.Loop loop = induction.loop();
      if ((!transformed.containsKey(loop) && transformed.size() == 2)
          || transformed.getOrDefault(loop, 0) >= 2
          || induction.phi().getNumOperands() != 4
          || loop.header().getPredecessors().size() != 2
          || containsCall(loop)
          || inductions.stream().anyMatch(other -> other.loop() != loop
              && loop.blocks().containsAll(other.loop().blocks()))) continue;
      for (BasicBlock block : function.getBlocks()) {
        if (!loop.blocks().contains(block)) continue;
        for (Instruction gep : java.util.List.copyOf(block.getInstructions())) {
          if (gep.getOpcode() != Instruction.Opcode.GEP
              || !AffineGepCandidate.isMemoryAddress(gep)) continue;
          int varyingIndex = AffineGepCandidate.varyingIndex(gep, induction.phi());
          if (varyingIndex < 1
              || !AffineGepCandidate.otherOperandsAreInvariant(gep, varyingIndex, loop)) continue;
          long byteStep = induction.step() * AffineGepCandidate.strideAt(gep, varyingIndex);
          if (byteStep % 4 != 0) continue;
          rewrite(gep, varyingIndex, byteStep / 4, induction);
          transformed.merge(loop, 1, Integer::sum);
          changed = true;
          if (transformed.get(loop) >= 2) break;
        }
        if (transformed.getOrDefault(loop, 0) >= 2) break;
      }
    }
    return changed;
  }

  private static void rewrite(
      Instruction gep,
      int varyingIndex,
      long pointerStep,
      InductionVariableAnalysis.Induction induction) {
    IRBuilder preheaderBuilder = new IRBuilder();
    preheaderBuilder.setInsertPointBefore(induction.loop().preheader().getTerminator());
    Value[] initialIndices = new Value[gep.getNumOperands() - 1];
    for (int index = 1; index < gep.getNumOperands(); index++) {
      initialIndices[index - 1] = index == varyingIndex
          ? extendStart(preheaderBuilder, gep.getOperand(index), induction.start())
          : materializeInvariant(preheaderBuilder, gep.getOperand(index), induction.loop());
    }
    Instruction initial = preheaderBuilder.createGEP(
        gep.getGepSourceType(), gep.getOperand(0), initialIndices, gep.isGepInbounds());
    Instruction pointer = Instruction.createPhi(Type.PTR);
    induction.loop().header().addInstructionToFront(pointer);
    pointer.addOperand(initial);
    pointer.addOperand(induction.loop().preheader());
    IRBuilder latchBuilder = new IRBuilder();
    latchBuilder.setInsertPointBefore(induction.loop().latch().getTerminator());
    Instruction next = latchBuilder.createGEP(
        Type.INT, pointer, new Value[] {Constant.int64Const(pointerStep)}, gep.isGepInbounds());
    pointer.addOperand(next);
    pointer.addOperand(induction.loop().latch());
    gep.replaceAllUsesWith(pointer);
    gep.eraseFromParent();
  }

  private static Value extendStart(IRBuilder builder, Value oldIndex, Value start) {
    if (oldIndex instanceof Instruction extension
        && extension.getOpcode() == Instruction.Opcode.SEXT
        && extension.getOperand(0).getType() == start.getType()) {
      return builder.createSExt(start, extension.getType());
    }
    return start;
  }

  private static Value materializeInvariant(
      IRBuilder builder, Value value, LoopAnalysis.Loop loop) {
    if (!(value instanceof Instruction instruction)
        || !loop.blocks().contains(instruction.getParent())) return value;
    Value operand = materializeInvariant(builder, instruction.getOperand(0), loop);
    return instruction.getOpcode() == Instruction.Opcode.SEXT
        ? builder.createSExt(operand, instruction.getType())
        : builder.createZExt(operand, instruction.getType());
  }

  private static boolean containsCall(LoopAnalysis.Loop loop) {
    return loop.blocks().stream().flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopAddressStrengthReduction.run(function, fam)
          ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

}
