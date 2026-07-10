package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Replaces one affine memory address per loop with a pointer recurrence. */
public final class LoopAddressStrengthReduction {
  private LoopAddressStrengthReduction() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    Set<LoopAnalysis.Loop> transformed =
        Collections.newSetFromMap(new IdentityHashMap<>());
    boolean changed = false;
    for (var induction : fam.getResult(
        InductionVariableAnalysis.class, function).inductions()) {
      LoopAnalysis.Loop loop = induction.loop();
      if (transformed.contains(loop) || induction.phi().getNumOperands() != 4
          || loop.header().getPredecessors().size() != 2) continue;
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
          transformed.add(loop);
          changed = true;
          break;
        }
        if (transformed.contains(loop)) break;
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
          : gep.getOperand(index);
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

}
