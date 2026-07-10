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

final class RISCVImmediateArithmeticTest {
  @Test
  void selectsSignedTwelveBitArithmeticImmediates() {
    Module module = new Module();
    Function function = new Function("arithmetic", Type.INT);
    module.addFunction(function);
    Value argument = function.addArgument(Type.INT, "argument");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value seven = Constant.intConst(7);
    builder.createAdd(argument, seven);
    builder.createAdd(seven, argument);
    builder.createSub(argument, seven);
    builder.createXor(argument, seven);
    Value result = builder.createXor(seven, argument);
    builder.createRet(result);

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertTrue(hasInstruction(assembly, "addi", "7"));
    assertTrue(hasInstruction(assembly, "addi", "-7"));
    assertTrue(hasInstruction(assembly, "xori", "7"));
    assertFalse(assembly.contains("  li t1, 7"));
    assertFalse(assembly.contains("  add t2, t0, t1"));
    assertFalse(assembly.contains("  sub t2, t0, t1"));
    assertFalse(assembly.contains("  xor t2, t0, t1"));
  }

  private static boolean hasInstruction(String assembly, String opcode, String immediate) {
    return assembly.lines().anyMatch(line ->
        line.matches("\\s+" + opcode + " [a-z0-9]+, [a-z0-9]+, " + immediate));
  }
}
