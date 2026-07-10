package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
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
}
