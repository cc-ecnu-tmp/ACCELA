package accela.pass.ir.transform.functionspecialization;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class FunctionSpecializationTest {
  @Test
  void clonesProfitableConflictingConstantCallGroups() {
    Module module = new Module();
    Function select = branchedFunction(module);
    Function input = new Function("getint", Type.INT);

    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    IRBuilder builder = new IRBuilder(main.addBlock("entry"));
    Value dynamic = builder.createCall(input, Type.INT);
    Instruction left = builder.createCall(
        select, Type.INT, Constant.boolConst(true), dynamic);
    Instruction right = builder.createCall(
        select, Type.INT, Constant.boolConst(false), dynamic);
    builder.createRet(builder.createAdd(left, right));

    assertTrue(FunctionSpecialization.runOnModule(module));

    assertEquals(4, module.getFunctions().size());
    assertNotSame(select, left.getCallee());
    assertNotSame(select, right.getCallee());
    assertNotSame(left.getCallee(), right.getCallee());
    assertEquals(1, left.getCallee().getBlocks().size());
    assertEquals(1, right.getCallee().getBlocks().size());
    assertEquals(0, left.getCallee().getArguments().get(0).getNumUses());
    assertEquals(0, right.getCallee().getArguments().get(0).getNumUses());
  }

  private static Function branchedFunction(Module module) {
    Function function = new Function("select", Type.INT);
    module.addFunction(function);
    Value flag = function.addArgument(Type.I1, "flag");
    Value value = function.addArgument(Type.INT, "value");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock left = function.addBlock("left");
    BasicBlock right = function.addBlock("right");
    new IRBuilder(entry).createCondBr(flag, left, right);
    buildBranch(left, value, true);
    buildBranch(right, value, false);
    return function;
  }

  private static void buildBranch(BasicBlock block, Value input, boolean add) {
    IRBuilder builder = new IRBuilder(block);
    Value value = input;
    for (int i = 0; i < 6; i++) {
      value = add
          ? builder.createAdd(value, Constant.intConst(i + 1))
          : builder.createSub(value, Constant.intConst(i + 1));
    }
    builder.createRet(value);
  }
}
