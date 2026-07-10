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
import java.util.Map;
import org.junit.jupiter.api.Test;

final class RISCVCompareBranchTest {
  @Test
  void fusesSingleUseIntegerComparisonsIntoBranches() {
    Map<String, String> expected =
        Map.of(
            "eq", "beq t0, zero",
            "ne", "bne t0, zero",
            "slt", "blt t0, zero",
            "sgt", "blt zero, t0",
            "sle", "bge zero, t0",
            "sge", "bge t0, zero");

    for (var entry : expected.entrySet()) {
      String assembly = compileBranch(entry.getKey());
      assertTrue(assembly.contains("  " + entry.getValue()), entry.getKey());
      assertFalse(assembly.contains("  bnez t0"), entry.getKey());
      assertFalse(assembly.contains("  seqz t2"), entry.getKey());
      assertFalse(assembly.contains("  snez t2"), entry.getKey());
    }
  }

  private static String compileBranch(String predicate) {
    Module module = new Module();
    Function function = new Function("branch_" + predicate, Type.INT);
    module.addFunction(function);
    Value argument = function.addArgument(Type.INT, "argument");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock ifTrue = function.addBlock("true");
    BasicBlock ifFalse = function.addBlock("false");
    IRBuilder builder = new IRBuilder(entry);
    Value condition = builder.createICmp(predicate, argument, Constant.intConst(0));
    builder.createCondBr(condition, ifTrue, ifFalse);
    new IRBuilder(ifTrue).createRet(Constant.intConst(1));
    new IRBuilder(ifFalse).createRet(Constant.intConst(0));
    return new BackendCompiler().compileToAssembly(module);
  }
}
