package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class ReadNoneCallCSETest {
  @Test
  void mergesRecursiveReadnoneCallsButNotMemoryReads() {
    Module module = new Module();
    Function pure = recursiveIdentity(module);
    GlobalVariable state = new GlobalVariable(
        "state", Type.INT, Constant.intConst(0), false);
    module.addGlobal(state);
    Function reader = new Function("reader", Type.INT);
    module.addFunction(reader);
    new IRBuilder(reader.addBlock("entry")).createRet(
        new IRBuilder(reader.getEntryBlock()).createLoad(Type.INT, state));

    Function caller = new Function("main", Type.INT);
    module.addFunction(caller);
    BasicBlock entry = caller.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value argument = Constant.intConst(7);
    Value condition = Constant.boolConst(false);
    Instruction first = builder.createCall(pure, Type.INT, argument, condition);
    Instruction duplicate = builder.createCall(pure, Type.INT, argument, condition);
    builder.createCall(reader, Type.INT);
    builder.createCall(reader, Type.INT);
    builder.createRet(builder.createAdd(first, duplicate));

    assertTrue(ReadNoneCallCSE.runOnModule(module));

    assertEquals(3, entry.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL).count());
    Instruction sum = entry.getInstructions().get(entry.getInstructions().size() - 2);
    assertSame(first, sum.getOperand(0));
    assertSame(first, sum.getOperand(1));
  }

  private static Function recursiveIdentity(Module module) {
    Function function = new Function("identity", Type.INT);
    module.addFunction(function);
    Value value = function.addArgument(Type.INT, "value");
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock recurse = function.addBlock("recurse");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createCondBr(condition, recurse, exit);
    IRBuilder recurseBuilder = new IRBuilder(recurse);
    recurseBuilder.createRet(recurseBuilder.createCall(
        function, Type.INT, value, condition));
    new IRBuilder(exit).createRet(value);
    return function;
  }
}
