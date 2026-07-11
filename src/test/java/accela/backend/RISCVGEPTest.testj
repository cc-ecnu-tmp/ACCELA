package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVGEPTest {
  @Test
  void foldsConstantAggregateIndicesIntoOneOffset() {
    Module module = new Module();
    Type row = Type.array(Type.INT, 8);
    Type matrix = Type.array(row, 4);
    GlobalVariable array = new GlobalVariable("array", matrix, Constant.zero(matrix), false);
    module.addGlobal(array);
    Function function = new Function("element", Type.INT);
    module.addFunction(function);
    IRBuilder builder = new IRBuilder(function.addBlock("entry"));
    Value pointer = builder.createGEP(
        matrix, array,
        new Value[] {Constant.int64Const(0), Constant.int64Const(2), Constant.int64Const(3)},
        true);
    builder.createRet(builder.createLoad(Type.INT, pointer));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertFalse(assembly.contains("  mul "));
    assertFalse(assembly.matches("(?s).*\\baddi [^\\n]+, [^\\n]+, 76\\b.*"));
    assertTrue(assembly.matches("(?s).*\\blw [^\\n]+, 76\\([^\\n]+\\).*"));
  }
}
