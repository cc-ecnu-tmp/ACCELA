package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVSiblingTailCallTest {
  @Test
  void emitsRegisterOnlyCallReturnAsTailCall() {
    Module module = new Module();
    Function callee = new Function("callee", Type.INT);
    module.addFunction(callee);
    Value calleeArgument = callee.addArgument(Type.INT, "argument");
    new IRBuilder(callee.addBlock("entry")).createRet(calleeArgument);
    Function caller = new Function("caller", Type.INT);
    module.addFunction(caller);
    Value callerArgument = caller.addArgument(Type.INT, "argument");
    IRBuilder builder = new IRBuilder(caller.addBlock("entry"));
    Value call = builder.createCall(callee, Type.INT, callerArgument);
    builder.createRet(call);

    String assembly = new BackendCompiler().compileToAssembly(module);

    assertTrue(assembly.contains("  tail callee"));
    assertFalse(assembly.contains("  call callee"));
  }
}
