package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Module;
import accela.ir.Type;
import java.util.Set;
import org.junit.jupiter.api.Test;

final class GlobalDCETest {
  @Test
  void removesOnlyFunctionsUnreachableFromMain() {
    Module module = new Module();
    Function reachable = constantFunction(module, "reachable", 7);
    Function dead = constantFunction(module, "dead", 9);

    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    IRBuilder builder = new IRBuilder(main.addBlock("entry"));
    builder.createCall(reachable, Type.INT);
    builder.createRet(Constant.intConst(0));

    assertTrue(GlobalDCE.runOnModule(module));
    assertEquals(Set.of(reachable, main), Set.copyOf(module.getFunctions()));
    assertFalse(module.getFunctions().contains(dead));
    assertNull(dead.getModule());
    assertTrue(dead.getBlocks().isEmpty());
  }

  private static Function constantFunction(Module module, String name, int result) {
    Function function = new Function(name, Type.INT);
    module.addFunction(function);
    new IRBuilder(function.addBlock("entry")).createRet(Constant.intConst(result));
    return function;
  }
}
