package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVRemainderStrengthReductionTest {
  @Test
  void reducesSignedRemaindersByPowersOfTwo() {
    Module module = new Module();
    Function function = new Function("remainder", Type.INT);
    module.addFunction(function);
    Value argument = function.addArgument(Type.INT, "argument");
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    builder.createRet(builder.createSRem(argument, Constant.intConst(8)));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertTrue(assembly.contains("  srliw t3, t3, 29"));
    assertTrue(assembly.contains("  andi "));
    assertFalse(assembly.contains("  remw "));
  }
}
