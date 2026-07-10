package accela.backend;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVCopyTest {
  @Test
  void extendsDirectlyIntoTheAllocatedRegister() {
    Module module = new Module();
    Function function = new Function("extend", Type.I64);
    module.addFunction(function);
    Value argument = function.addArgument(Type.INT, "argument");
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    builder.createRet(builder.createSExt(argument, Type.I64));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertEquals(2, assembly.lines().filter(line -> line.startsWith("  mv ")).count());
    assertFalse(assembly.contains("  mv t0,"));
  }

  @Test
  void loadsDirectlyIntoTheAllocatedRegister() {
    Module module = new Module();
    GlobalVariable global =
        new GlobalVariable("global", Type.INT, accela.ir.Constant.intConst(7), false);
    module.addGlobal(global);
    Function function = new Function("load", Type.INT);
    module.addFunction(function);
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    builder.createRet(builder.createLoad(Type.INT, global));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertFalse(assembly.contains("  lw t1,"));
  }
}
