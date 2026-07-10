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
  private static final int MAX_TRANSFORMED_LOOPS = 8;
  private static final int MAX_RECURRENCES_PER_LOOP = 8;

  private LoopAddressStrengthReduction() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    Map<LoopAnalysis.Loop, Integer> transformed = new IdentityHashMap<>();
    boolean changed = false;
    var inductions = fam.getResult(InductionVariableAnalysis.class, function).inductions();
    for (var induction : inductions) {
      LoopAnalysis.Loop loop = induction.loop();
      if ((!transformed.containsKey(loop) && transformed.size() == MAX_TRANSFORMED_LOOPS)
          || transformed.getOrDefault(loop, 0) >= MAX_RECURRENCES_PER_LOOP
          || induction.phi().getNumOperands() != 4
          || loop.header().getPredecessors().size() != 2
          || containsCall(loop)) continue;
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
          var equivalents = equivalentAddresses(function, loop, gep);
          Instruction pointer = rewrite(gep, varyingIndex, byteStep / 4, induction);
          for (Instruction equivalent : equivalents) {
            equivalent.replaceAllUsesWith(pointer);
            equivalent.eraseFromParent();
          }
          transformed.merge(loop, 1, Integer::sum);
          changed = true;
          if (transformed.get(loop) >= MAX_RECURRENCES_PER_LOOP) break;
        }
        if (transformed.getOrDefault(loop, 0) >= MAX_RECURRENCES_PER_LOOP) break;
      }
    }
    return changed;
  }

  private static java.util.List<Instruction> equivalentAddresses(
      Function function, LoopAnalysis.Loop loop, Instruction address) {
    java.util.List<Instruction> result = new java.util.ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      if (!loop.blocks().contains(block)) continue;
      for (Instruction candidate : block.getInstructions()) {
        if (candidate != address && candidate.getOpcode() == Instruction.Opcode.GEP
            && AffineGepCandidate.sameAddressExpression(address, candidate)) {
          result.add(candidate);
        }
      }
    }
    return result;
  }

  private static Instruction rewrite(
      Instruction gep,
      int varyingIndex,
      long pointerStep,
      InductionVariableAnalysis.Induction induction) {
    IRBuilder preheaderBuilder = new IRBuilder();
    preheaderBuilder.setInsertPointBefore(induction.loop().preheader().getTerminator());
    Value[] initialIndices = new Value[gep.getNumOperands() - 1];
    for (int index = 1; index < gep.getNumOperands(); index++) {
      initialIndices[index - 1] = index == varyingIndex
          ? extendStart(
              preheaderBuilder, gep.getOperand(index), induction.start(), induction.phi())
          : materializeInvariant(preheaderBuilder, gep.getOperand(index), induction.loop());
    }
    Instruction initial = preheaderBuilder.createGEP(
        gep.getGepSourceType(),
        materializeInvariant(preheaderBuilder, gep.getOperand(0), induction.loop()),
        initialIndices, gep.isGepInbounds());
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
    return pointer;
  }

  private static Value extendStart(
      IRBuilder builder, Value oldIndex, Value start, Instruction induction) {
    Long offset = AffineGepCandidate.inductionOffset(oldIndex, induction);
    if (offset == null) return start;
    Value initial = offset == 0 ? start
        : builder.createAdd(start, Constant.intConst(offset));
    if (oldIndex instanceof Instruction extension
        && extension.getOpcode() == Instruction.Opcode.SEXT
        && extension.getType() != start.getType()) {
      return builder.createSExt(initial, extension.getType());
    }
    return initial;
  }

  private static Value materializeInvariant(
      IRBuilder builder, Value value, LoopAnalysis.Loop loop) {
    if (!(value instanceof Instruction instruction)
        || !loop.blocks().contains(instruction.getParent())) return value;
    if (instruction.getOpcode() == Instruction.Opcode.GEP) {
      Value[] indices = new Value[instruction.getNumOperands() - 1];
      for (int index = 1; index < instruction.getNumOperands(); index++) {
        indices[index - 1] = materializeInvariant(builder, instruction.getOperand(index), loop);
      }
      return builder.createGEP(
          instruction.getGepSourceType(),
          materializeInvariant(builder, instruction.getOperand(0), loop),
          indices, instruction.isGepInbounds());
    }
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
