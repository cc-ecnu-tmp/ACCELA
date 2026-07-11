package accela.backend;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import org.junit.jupiter.api.Test;

final class LargeMemzeroTest {
  @Test
  void callsMemsetAndPreservesReturnAddressAboveThreshold() {
    String assembly = compileZeroFill(9);

    assertTrue(assembly.contains("  call memset"));
    assertTrue(assembly.contains("  sd ra,"));
    assertTrue(assembly.contains("  ld ra,"));
  }

  @Test
  void keepsSmallZeroFillsInline() {
    String assembly = compileZeroFill(8);

    assertFalse(assembly.contains("call memset"));
    assertEquals(8, assembly.lines().filter(line -> line.startsWith("  sw zero,")).count());
  }

  private static String compileZeroFill(int elements) {
    Module module = new Module();
    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    BasicBlock entry = main.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Type arrayType = Type.array(Type.INT, elements);
    Instruction storage = builder.createAlloca(arrayType);
    builder.createStore(Constant.zero(arrayType), storage);
    builder.createRet(Constant.intConst(0));
    return new BackendCompiler().compileToAssembly(module);
  }
}
