package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVCompareTest {
  @Test
  void selectsZeroCompareInstructionsWithoutMaterializingZero() {
    Module module = new Module();
    Function function = new Function("compare", Type.I1);
    module.addFunction(function);
    Value argument = function.addArgument(Type.INT, "argument");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value result = null;
    for (String predicate : new String[] {"eq", "ne", "slt", "sgt", "sle", "sge"}) {
      result = builder.createICmp(predicate, argument, Constant.intConst(0));
    }
    builder.createRet(result);

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertTrue(assembly.contains("  seqz t2, t0"));
    assertTrue(assembly.contains("  snez t2, t0"));
    assertTrue(assembly.contains("  slt t2, t0, zero"));
    assertTrue(assembly.contains("  slt t2, zero, t0"));
    assertFalse(assembly.contains("  li t1, 0"));
    assertFalse(assembly.contains("  sub t2, t0, t1"));
  }

  @Test
  void selectsSignedTwelveBitCompareImmediates() {
    Module module = new Module();
    Function function = new Function("compare", Type.I1);
    module.addFunction(function);
    Value argument = function.addArgument(Type.INT, "argument");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value result = null;
    for (String predicate : new String[] {"eq", "ne", "slt", "sgt", "sle", "sge"}) {
      result = builder.createICmp(predicate, argument, Constant.intConst(7));
    }
    builder.createRet(result);

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertTrue(assembly.contains("  addi t2, t0, -7"));
    assertTrue(assembly.contains("  slti t2, t0, 7"));
    assertTrue(assembly.contains("  slti t2, t0, 8"));
    assertFalse(assembly.contains("  li t1, 7"));
    assertFalse(assembly.contains("  sub t2, t0, t1"));
    assertFalse(assembly.contains("  slt t2, t0, t1"));
  }
}
