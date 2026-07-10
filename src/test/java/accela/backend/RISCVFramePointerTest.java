package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVFramePointerTest {
  @Test
  void readsStackArgumentsRelativeToTheStableStackPointer() {
    Module module = new Module();
    Function function = new Function("ninth", Type.INT);
    module.addFunction(function);
    Value argument = null;
    for (int i = 0; i < 9; i++) argument = function.addArgument(Type.INT, "arg" + i);
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    builder.createAlloca(Type.INT);
    builder.createRet(argument);

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertFalse(assembly.contains("s0"));
    assertTrue(assembly.matches("(?s).*\\blw [^\\n]+, 16\\(sp\\).*"));
  }
}
