package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class FoldPureConstantCallsTest {
  @Test
  void foldsAllConstantPureCall() {
    Module module = new Module();
    Function callee = new Function("callee", Type.INT);
    module.addFunction(callee);
    Value argument = callee.addArgument(Type.INT, "argument");
    IRBuilder calleeBuilder = new IRBuilder(callee.addBlock("entry"));
    calleeBuilder.createRet(calleeBuilder.createMul(argument, Constant.intConst(3)));

    Function caller = new Function("main", Type.INT);
    module.addFunction(caller);
    BasicBlock entry = caller.addBlock("entry");
    IRBuilder callerBuilder = new IRBuilder(entry);
    Value call = callerBuilder.createCall(callee, Type.INT, Constant.intConst(7));
    callerBuilder.createRet(call);

    FoldPureConstantCalls.runOnModule(module);

    assertEquals(1, entry.getInstructions().size());
    Instruction ret = entry.getInstructions().get(0);
    Constant.Int result = assertInstanceOf(Constant.Int.class, ret.getOperand(0));
    assertEquals(21, result.value);
  }
}
