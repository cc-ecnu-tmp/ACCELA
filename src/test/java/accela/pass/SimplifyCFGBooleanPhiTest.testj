package accela.pass;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.transform.SimplifyCFG;
import org.junit.jupiter.api.Test;

final class SimplifyCFGBooleanPhiTest {
  @Test
  void foldsBooleanPhiToCondition() {
    Function function = booleanDiamond(1, 0);

    assertTrue(SimplifyCFG.runOnFunction(function));
    assertEquals(1, function.getBlocks().size());
    assertEquals(0, count(function, Instruction.Opcode.PHI));
    assertEquals(0, count(function, Instruction.Opcode.CONDBR));
    assertEquals(1, count(function, Instruction.Opcode.ZEXT));
    assertEquals(0, count(function, Instruction.Opcode.XOR));
  }

  @Test
  void foldsInvertedBooleanPhiToNegatedCondition() {
    Function function = booleanDiamond(0, 1);

    assertTrue(SimplifyCFG.runOnFunction(function));
    assertEquals(1, function.getBlocks().size());
    assertEquals(0, count(function, Instruction.Opcode.PHI));
    assertEquals(0, count(function, Instruction.Opcode.CONDBR));
    assertEquals(1, count(function, Instruction.Opcode.ZEXT));
    assertEquals(1, count(function, Instruction.Opcode.XOR));
  }

  private static Function booleanDiamond(int directBit, int indirectBit) {
    Function function = new Function("f", Type.INT);
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock indirect = function.addBlock("indirect");
    BasicBlock merge = function.addBlock("merge");

    new IRBuilder(entry).createCondBr(condition, merge, indirect);
    new IRBuilder(indirect).createBr(merge);
    IRBuilder builder = new IRBuilder(merge);
    Instruction phi = builder.createPhi(Type.INT);
    phi.addOperand(Constant.intConst(directBit));
    phi.addOperand(entry);
    phi.addOperand(Constant.intConst(indirectBit));
    phi.addOperand(indirect);
    builder.createRet(phi);
    return function;
  }

  private static long count(Function function, Instruction.Opcode opcode) {
    return function.getBlocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == opcode)
        .count();
  }
}
