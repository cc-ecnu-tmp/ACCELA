package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import org.junit.jupiter.api.Test;

final class GlobalConstantPropagationTest {
  @Test
  void foldsOnlyLoadsFromConstantGlobals() {
    Module module = new Module();
    Constant initializer = Constant.intConst(998244353);
    GlobalVariable constant = new GlobalVariable("mod", Type.INT, initializer, true);
    GlobalVariable mutable = new GlobalVariable(
        "state", Type.INT, Constant.intConst(1), false);
    Constant readonlyInitializer = Constant.intConst(7);
    GlobalVariable readonly = new GlobalVariable(
        "readonly", Type.INT, readonlyInitializer, false);
    module.addGlobal(constant);
    module.addGlobal(mutable);
    module.addGlobal(readonly);
    Function function = new Function("read", Type.INT);
    module.addFunction(function);
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    Instruction constantLoad = builder.createLoad(Type.INT, constant);
    builder.createStore(Constant.intConst(2), mutable);
    Instruction mutableLoad = builder.createLoad(Type.INT, mutable);
    Instruction readonlyLoad = builder.createLoad(Type.INT, readonly);
    Instruction sum = builder.createAdd(constantLoad, mutableLoad);
    Instruction total = builder.createAdd(sum, readonlyLoad);
    builder.createRet(total);

    assertTrue(GlobalConstantPropagation.runOnModule(module));

    assertSame(initializer, sum.getOperand(0));
    assertSame(mutableLoad, sum.getOperand(1));
    assertSame(readonlyInitializer, total.getOperand(1));
    assertEquals(5, function.getEntryBlock().getInstructions().size());
    assertTrue(constant.getUses().isEmpty());
  }
}
