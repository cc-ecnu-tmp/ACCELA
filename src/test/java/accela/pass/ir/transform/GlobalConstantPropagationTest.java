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
    module.addGlobal(constant);
    module.addGlobal(mutable);
    Function function = new Function("read", Type.INT);
    module.addFunction(function);
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    Instruction constantLoad = builder.createLoad(Type.INT, constant);
    Instruction mutableLoad = builder.createLoad(Type.INT, mutable);
    Instruction sum = builder.createAdd(constantLoad, mutableLoad);
    builder.createRet(sum);

    assertTrue(GlobalConstantPropagation.runOnModule(module));

    assertSame(initializer, sum.getOperand(0));
    assertSame(mutableLoad, sum.getOperand(1));
    assertEquals(3, function.getEntryBlock().getInstructions().size());
    assertTrue(constant.getUses().isEmpty());
  }
}
