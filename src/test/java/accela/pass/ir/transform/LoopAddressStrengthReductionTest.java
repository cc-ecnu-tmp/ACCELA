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
  void replacesAffineMemoryAddressesWithPointerRecurrences() {
    Function function = new Function("walk", Type.VOID);
    Value condition = function.addArgument(Type.I1, "condition");
    Value base = function.addArgument(Type.PTR, "base");
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
    Instruction address = bodyBuilder.createGEP(
        Type.INT, base, new Value[] {index}, true);
    Instruction load = bodyBuilder.createLoad(Type.INT, address);
    Instruction secondAddress = bodyBuilder.createGEP(
        Type.INT, base, new Value[] {index}, true);
    Instruction secondLoad = bodyBuilder.createLoad(Type.INT, secondAddress);
    bodyBuilder.createLoad(Type.INT,
        bodyBuilder.createGEP(Type.INT, base, new Value[] {index}, true));
    Instruction fourthLoad = bodyBuilder.createLoad(Type.INT,
        bodyBuilder.createGEP(Type.INT, base, new Value[] {index}, true));
    Instruction next = bodyBuilder.createAdd(induction, Constant.intConst(1));
    bodyBuilder.createBr(header);
    induction.addOperand(next);
    induction.addOperand(body);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());

    assertTrue(LoopAddressStrengthReduction.run(function, fam));
    IRVerifier.verifyFunction(function);

    assertTrue(load.getOperand(0) instanceof Instruction pointer
        && pointer.getOpcode() == Instruction.Opcode.PHI);
    Instruction pointer = (Instruction) load.getOperand(0);
    assertSame(header, pointer.getParent());
    assertEquals(4, pointer.getNumOperands());
    assertSame(entry, ((Instruction) pointer.getOperand(0)).getParent());
    assertTrue(secondLoad.getOperand(0) instanceof Instruction secondPointer
        && secondPointer.getOpcode() == Instruction.Opcode.PHI);
    assertTrue(fourthLoad.getOperand(0) instanceof Instruction fourthPointer
        && fourthPointer.getOpcode() == Instruction.Opcode.PHI);
  }
}
