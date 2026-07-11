package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVFloatDestinationTest {
  @Test
  void computesFloatingResultsInTheAllocatedRegister() {
    Module module = new Module();
    Function function = new Function("float_result", Type.FLOAT);
    module.addFunction(function);
    Value argument = function.addArgument(Type.FLOAT, "argument");
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    Value sum = builder.createFAdd(argument, Constant.floatConst(1.0f));
    builder.createRet(builder.createFNeg(sum));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertFalse(assembly.contains("  fadd.s ft2,"));
    assertFalse(assembly.contains("  fneg.s ft1,"));
  }
}
