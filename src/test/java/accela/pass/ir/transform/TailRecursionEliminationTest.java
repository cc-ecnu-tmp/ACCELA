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
import accela.pass.ir.verify.IRVerifier;
import org.junit.jupiter.api.Test;

final class TailRecursionEliminationTest {
  @Test
  void turnsReturnedSelfCallsIntoBackedges() {
    Function function = new Function("count", Type.INT);
    Value argument = function.addArgument(Type.INT, "n");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock recur = function.addBlock("recur");
    BasicBlock exit = function.addBlock("exit");
    IRBuilder entryBuilder = new IRBuilder(entry);
    entryBuilder.createCondBr(
        entryBuilder.createICmp("eq", argument, Constant.intConst(0)), exit, recur);
    IRBuilder recurBuilder = new IRBuilder(recur);
    Instruction call = recurBuilder.createCall(
        function, Type.INT, recurBuilder.createSub(argument, Constant.intConst(1)));
    recurBuilder.createRet(call);
    new IRBuilder(exit).createRet(Constant.intConst(0));

    assertTrue(TailRecursionElimination.run(function));
    IRVerifier.verifyFunction(function);

    assertEquals(Instruction.Opcode.PHI, entry.getInstructions().get(0).getOpcode());
    assertEquals(Instruction.Opcode.BR, recur.getTerminator().getOpcode());
    assertSame(entry, recur.getTerminator().getOperand(0));
  }
}
