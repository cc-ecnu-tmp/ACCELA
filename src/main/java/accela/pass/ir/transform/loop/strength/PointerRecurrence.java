package accela.pass.ir.transform.loop.strength;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.InductionVariableAnalysis;

/** Rebuilds one affine GEP as a loop-carried pointer PHI. */
final class PointerRecurrence {
  record Result(Instruction initial, Instruction pointer, Instruction next, long elementStep) {}

  private PointerRecurrence() {}

  static Result rewrite(
      Instruction gep,
      int varyingIndex,
      long elementStep,
      InductionVariableAnalysis.Induction induction) {
    BasicBlock preheader = induction.predecessor();
    Instruction preheaderTerminator = preheader.getTerminator();
    Instruction insertionPoint = preheaderTerminator;
    Value[] initialIndices = new Value[gep.getNumOperands() - 1];
    for (int index = 1; index < gep.getNumOperands(); index++) {
      Value operand = gep.getOperand(index);
      initialIndices[index - 1] = index == varyingIndex
          ? substituteStart(operand, induction, preheader, insertionPoint)
          : materializeInvariant(operand, induction, preheader, insertionPoint);
    }

    IRBuilder preheaderBuilder = new IRBuilder();
    preheaderBuilder.setInsertPointBefore(insertionPoint);
    Instruction initial = preheaderBuilder.createGEP(
        gep.getGepSourceType(),
        materializeInvariant(
            gep.getOperand(0), induction, preheader, insertionPoint),
        initialIndices,
        false);

    Instruction pointer = Instruction.createPhi(Type.PTR);
    induction.loop().header().addInstructionToFront(pointer);
    pointer.addOperand(initial);
    pointer.addOperand(preheader);

    IRBuilder latchBuilder = new IRBuilder();
    latchBuilder.setInsertPointBefore(induction.latch().getTerminator());
    Instruction next = latchBuilder.createGEP(
        Type.INT,
        pointer,
        new Value[] {Constant.int64Const(elementStep)},
        false);
    pointer.addOperand(next);
    pointer.addOperand(induction.latch());

    gep.replaceAllUsesWith(pointer);
    gep.eraseFromParent();
    return new Result(initial, pointer, next, elementStep);
  }

  static void rewriteOffset(Instruction gep, Value pointer, long byteOffset) {
    Value replacement = pointer;
    if (byteOffset != 0) {
      IRBuilder builder = new IRBuilder();
      builder.setInsertPointBefore(gep);
      replacement = builder.createGEP(
          Type.INT,
          pointer,
          new Value[] {Constant.int64Const(byteOffset / Integer.BYTES)},
          false);
    }
    gep.replaceAllUsesWith(replacement);
    gep.eraseFromParent();
  }

  private static Value substituteStart(
      Value value,
      InductionVariableAnalysis.Induction induction,
      BasicBlock block,
      Instruction before) {
    if (value == induction.phi()) return induction.start();
    Instruction expression = (Instruction) value;
    if (isExtension(expression)) {
      Value operand = substituteStart(
          expression.getOperand(0), induction, block, before);
      if (operand instanceof Constant.Int constant && expression.getType() == Type.I64) {
        long extended = expression.getOpcode() == Instruction.Opcode.SEXT
            ? (int) constant.value : constant.value & 0xffff_ffffL;
        return Constant.int64Const(extended);
      }
      return cloneBefore(
          expression,
          block,
          before,
          operand);
    }
    boolean leftVaries =
        AffineGep.isAffine(expression.getOperand(0), induction.phi(), induction.loop());
    Value left = leftVaries
        ? substituteStart(expression.getOperand(0), induction, block, before)
        : materializeInvariant(expression.getOperand(0), induction, block, before);
    Value right = leftVaries
        ? materializeInvariant(expression.getOperand(1), induction, block, before)
        : substituteStart(expression.getOperand(1), induction, block, before);
    return cloneBefore(expression, block, before, left, right);
  }

  private static Value materializeInvariant(
      Value value,
      InductionVariableAnalysis.Induction induction,
      BasicBlock block,
      Instruction before) {
    if (!(value instanceof Instruction instruction)
        || !induction.loop().contains(instruction.getParent())) return value;
    Value[] operands = new Value[instruction.getNumOperands()];
    for (int index = 0; index < operands.length; index++) {
      operands[index] =
          materializeInvariant(instruction.getOperand(index), induction, block, before);
    }
    return cloneBefore(instruction, block, before, operands);
  }

  private static Instruction cloneBefore(
      Instruction source, BasicBlock block, Instruction before, Value... operands) {
    Instruction clone = source.copyWithoutOperands();
    for (Value operand : operands) clone.addOperand(operand);
    block.insertInstructionBefore(before, clone);
    return clone;
  }

  private static boolean isExtension(Instruction instruction) {
    return instruction.getOpcode() == Instruction.Opcode.SEXT
        || instruction.getOpcode() == Instruction.Opcode.ZEXT;
  }
}
