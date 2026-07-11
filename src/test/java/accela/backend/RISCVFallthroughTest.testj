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

final class RISCVFallthroughTest {
  @Test
  void omitsJumpsToTheNextBlock() {
    Module module = new Module();
    Function function = new Function("fallthrough", Type.INT);
    module.addFunction(function);
    BasicBlock entry = function.addBlock("entry");
    BasicBlock next = function.addBlock("next");
    new IRBuilder(entry).createBr(next);
    new IRBuilder(next).createRet(Constant.intConst(0));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertFalse(assembly.contains("  j .L_fallthrough_next"));
  }

  @Test
  void invertsBranchesWhenTheTrueBlockIsNext() {
    Module module = new Module();
    Function function = new Function("conditional_fallthrough", Type.INT);
    module.addFunction(function);
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock ifTrue = function.addBlock("true");
    BasicBlock ifFalse = function.addBlock("false");
    new IRBuilder(entry).createCondBr(condition, ifTrue, ifFalse);
    new IRBuilder(ifTrue).createRet(Constant.intConst(1));
    new IRBuilder(ifFalse).createRet(Constant.intConst(0));

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertTrue(assembly.contains("  beqz "));
    assertFalse(assembly.contains("  j .L_conditional_fallthrough_false"));
  }
}
