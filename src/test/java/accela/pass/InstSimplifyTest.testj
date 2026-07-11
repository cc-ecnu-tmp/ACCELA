package accela.pass;

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
import accela.pass.ir.transform.instsimplify.InstSimplify;
import org.junit.jupiter.api.Test;

final class InstSimplifyTest {
  @Test
  void reducesPowerOfTwoRemainderComparedWithZero() {
    Function function = new Function("even", Type.I1);
    Value input = function.addArgument(Type.INT, "input");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value remainder = builder.createSRem(input, Constant.intConst(2));
    Instruction compare = builder.createICmp("eq", remainder, Constant.intConst(0));
    builder.createRet(compare);

    assertTrue(InstSimplify.runOnFunction(function));

    assertEquals(0, count(entry, Instruction.Opcode.SREM));
    assertEquals(1, count(entry, Instruction.Opcode.AND));
    Instruction mask = (Instruction) compare.getOperand(0);
    assertSame(input, mask.getOperand(0));
  }

  @Test
  void removesBooleanExtensionComparedWithZero() {
    Function function = new Function("boolean", Type.I1);
    Value input = function.addArgument(Type.I1, "input");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value extension = builder.createZExt(input, Type.INT);
    Value compare = builder.createICmp("ne", extension, Constant.intConst(0));
    Instruction ret = builder.createRet(compare);

    assertTrue(InstSimplify.runOnFunction(function));

    assertSame(input, ret.getOperand(0));
    assertEquals(0, count(entry, Instruction.Opcode.ICMP));
    assertEquals(0, count(entry, Instruction.Opcode.ZEXT));
  }

  private static long count(BasicBlock block, Instruction.Opcode opcode) {
    return block.getInstructions().stream().filter(i -> i.getOpcode() == opcode).count();
  }
}
