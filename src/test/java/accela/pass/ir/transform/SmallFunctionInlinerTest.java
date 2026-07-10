package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.verify.IRVerifier;
import org.junit.jupiter.api.Test;

final class SmallFunctionInlinerTest {
  @Test
  void inlinesStraightLineLeafCalls() {
    Module module = new Module();
    Function increment = new Function("increment", Type.INT);
    module.addFunction(increment);
    Value argument = increment.addArgument(Type.INT, "value");
    IRBuilder incrementBuilder = new IRBuilder(increment.addBlock("entry"));
    incrementBuilder.createRet(
        incrementBuilder.createAdd(argument, Constant.intConst(1)));
    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    IRBuilder mainBuilder = new IRBuilder(main.addBlock("entry"));
    Instruction call = mainBuilder.createCall(increment, Type.INT, Constant.intConst(41));
    Instruction ret = mainBuilder.createRet(call);

    assertTrue(SmallFunctionInliner.runOnModule(module));
    IRVerifier.verifyModule(module);

    assertEquals(2, main.getEntryBlock().getInstructions().size());
    assertEquals(Instruction.Opcode.ADD,
        main.getEntryBlock().getInstructions().get(0).getOpcode());
    assertSame(main.getEntryBlock().getInstructions().get(0), ret.getOperand(0));
  }
}
