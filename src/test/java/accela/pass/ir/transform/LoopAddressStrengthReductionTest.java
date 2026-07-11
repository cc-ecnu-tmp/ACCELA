package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.verify.IRVerifier;
import org.junit.jupiter.api.Test;

final class LoopAddressStrengthReductionTest {
  @Test
  void reducesOuterAddressesAcrossInnerLoops() {
    Function function = new Function("nested", Type.VOID);
    Value condition = function.addArgument(Type.I1, "condition");
    Value base = function.addArgument(Type.PTR, "base");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock outerHeader = function.addBlock("outer.header");
    BasicBlock outerBody = function.addBlock("outer.body");
    BasicBlock innerHeader = function.addBlock("inner.header");
    BasicBlock innerBody = function.addBlock("inner.body");
    BasicBlock outerLatch = function.addBlock("outer.latch");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(outerHeader);

    Instruction outer = Instruction.createPhi(Type.INT);
    outerHeader.addInstructionToFront(outer);
    outer.addOperand(Constant.intConst(0));
    outer.addOperand(entry);
    new IRBuilder(outerHeader).createCondBr(condition, outerBody, exit);
    IRBuilder outerBuilder = new IRBuilder(outerBody);
    Instruction address = outerBuilder.createGEP(Type.INT, base,
        new Value[] {outerBuilder.createSExt(outer, Type.I64)}, true);
    Instruction load = outerBuilder.createLoad(Type.INT, address);
    outerBuilder.createBr(innerHeader);

    Instruction inner = Instruction.createPhi(Type.INT);
    innerHeader.addInstructionToFront(inner);
    inner.addOperand(Constant.intConst(0));
    inner.addOperand(outerBody);
    new IRBuilder(innerHeader).createCondBr(condition, innerBody, outerLatch);
    IRBuilder innerBuilder = new IRBuilder(innerBody);
    Instruction innerNext = innerBuilder.createAdd(inner, Constant.intConst(1));
    innerBuilder.createBr(innerHeader);
    inner.addOperand(innerNext);
    inner.addOperand(innerBody);

    IRBuilder latchBuilder = new IRBuilder(outerLatch);
    Instruction outerNext = latchBuilder.createAdd(outer, Constant.intConst(1));
    latchBuilder.createBr(outerHeader);
    outer.addOperand(outerNext);
    outer.addOperand(outerLatch);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = analysisManager();

    assertTrue(LoopAddressStrengthReduction.run(function, fam));
    IRVerifier.verifyFunction(function);
    assertTrue(load.getOperand(0) instanceof Instruction pointer
        && pointer.getOpcode() == Instruction.Opcode.PHI);
    assertSame(outerHeader, ((Instruction) load.getOperand(0)).getParent());
  }

  @Test
  void replacesAffineMemoryAddressesWithPointerRecurrences() {
    Function function = new Function("walk", Type.VOID);
    Value condition = function.addArgument(Type.I1, "condition");
    Value base = function.addArgument(Type.PTR, "base");
    Value offset = function.addArgument(Type.I64, "offset");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock header = function.addBlock("header");
    BasicBlock body = function.addBlock("body");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    Instruction induction = Instruction.createPhi(Type.INT);
    header.addInstructionToFront(induction);
    induction.addOperand(Constant.intConst(0));
    induction.addOperand(entry);
    new IRBuilder(header).createCondBr(condition, body, exit);
    IRBuilder bodyBuilder = new IRBuilder(body);
    Instruction index = bodyBuilder.createSExt(induction, Type.I64);
    Instruction affineIndex = bodyBuilder.createAdd(index, offset);
    Type rowType = Type.array(Type.INT, 16);
    Instruction invariantRow = bodyBuilder.createSub(offset, Constant.int64Const(1));
    Instruction row = bodyBuilder.createGEP(
        rowType, base, new Value[] {invariantRow}, true);
    Instruction address = bodyBuilder.createGEP(
        Type.INT, row, new Value[] {affineIndex}, true);
    Instruction load = bodyBuilder.createLoad(Type.INT, address);
    Instruction secondAddress = bodyBuilder.createGEP(
        Type.INT, row, new Value[] {bodyBuilder.createAdd(index, offset)}, true);
    Instruction secondLoad = bodyBuilder.createLoad(Type.INT, secondAddress);
    Instruction nearbyAddress = bodyBuilder.createGEP(
        Type.INT, row,
        new Value[] {bodyBuilder.createAdd(affineIndex, Constant.int64Const(1))}, true);
    Instruction nearbyLoad = bodyBuilder.createLoad(Type.INT, nearbyAddress);
    bodyBuilder.createLoad(Type.INT,
        bodyBuilder.createGEP(Type.INT, row, new Value[] {index}, true));
    Instruction offsetIndex = bodyBuilder.createSExt(
        bodyBuilder.createAdd(induction, Constant.intConst(1)), Type.I64);
    Instruction offsetRow = bodyBuilder.createGEP(
        rowType, base, new Value[] {offsetIndex}, true);
    Instruction fourthAddress = bodyBuilder.createGEP(
        Type.INT, offsetRow, new Value[] {Constant.int64Const(0)}, true);
    Instruction fourthLoad = bodyBuilder.createLoad(Type.INT, fourthAddress);
    Instruction next = bodyBuilder.createAdd(induction, Constant.intConst(1));
    bodyBuilder.createBr(header);
    induction.addOperand(next);
    induction.addOperand(body);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = analysisManager();

    assertTrue(LoopAddressStrengthReduction.run(function, fam));
    IRVerifier.verifyFunction(function);

    assertTrue(load.getOperand(0) instanceof Instruction pointer
        && pointer.getOpcode() == Instruction.Opcode.PHI);
    Instruction pointer = (Instruction) load.getOperand(0);
    assertSame(pointer, secondLoad.getOperand(0));
    assertTrue(nearbyLoad.getOperand(0) instanceof Instruction nearbyPointer
        && nearbyPointer.getOpcode() == Instruction.Opcode.GEP);
    Instruction nearbyPointer = (Instruction) nearbyLoad.getOperand(0);
    assertSame(pointer, nearbyPointer.getOperand(0));
    assertEquals(1, ((Constant.Int) nearbyPointer.getOperand(1)).value);
    assertSame(header, pointer.getParent());
    assertEquals(4, pointer.getNumOperands());
    assertSame(entry, ((Instruction) pointer.getOperand(0)).getParent());
    assertTrue(secondLoad.getOperand(0) instanceof Instruction secondPointer
        && secondPointer.getOpcode() == Instruction.Opcode.PHI);
    assertSame(fourthAddress, fourthLoad.getOperand(0));
    assertTrue(fourthAddress.getOperand(0) instanceof Instruction fourthPointer
        && fourthPointer.getOpcode() == Instruction.Opcode.PHI);
  }

  private static FunctionAnalysisManager analysisManager() {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    return fam;
  }
}
