package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertSame;
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

final class IPSCCPSolverTest {
  @Test
  void propagatesArgumentsAndReturnsThroughExecutableBranches() {
    Module module = new Module();
    Function choose = new Function("choose", Type.INT);
    module.addFunction(choose);
    Value flag = choose.addArgument(Type.I1, "flag");
    Value value = choose.addArgument(Type.INT, "value");
    BasicBlock entry = choose.addBlock("entry");
    BasicBlock selected = choose.addBlock("selected");
    BasicBlock other = choose.addBlock("other");
    new IRBuilder(entry).createCondBr(flag, selected, other);
    IRBuilder selectedBuilder = new IRBuilder(selected);
    selectedBuilder.createRet(selectedBuilder.createAdd(value, Constant.intConst(1)));
    new IRBuilder(other).createRet(Constant.intConst(99));

    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    BasicBlock mainEntry = main.addBlock("entry");
    IRBuilder mainBuilder = new IRBuilder(mainEntry);
    Instruction call = mainBuilder.createCall(
        choose, Type.INT, Constant.boolConst(true), Constant.intConst(41));
    Instruction ret = mainBuilder.createRet(call);

    assertTrue(new IPSCCPSolver(module).solve());
    assertEquals(Instruction.Opcode.BR, entry.getInstructions().get(0).getOpcode());
    Constant.Int result = assertInstanceOf(Constant.Int.class, ret.getOperand(0));
    assertEquals(42, result.value);
    assertSame(call, mainEntry.getInstructions().get(0));
  }

  @Test
  void joinsConflictingCallArgumentsToOverdefined() {
    Module module = new Module();
    Function identity = new Function("identity", Type.INT);
    module.addFunction(identity);
    Value argument = identity.addArgument(Type.INT, "argument");
    new IRBuilder(identity.addBlock("entry")).createRet(argument);

    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    IRBuilder builder = new IRBuilder(main.addBlock("entry"));
    Instruction one = builder.createCall(identity, Type.INT, Constant.intConst(1));
    Instruction two = builder.createCall(identity, Type.INT, Constant.intConst(2));
    Instruction sum = builder.createAdd(one, two);
    builder.createRet(sum);

    new IPSCCPSolver(module).solve();

    assertSame(one, sum.getOperand(0));
    assertSame(two, sum.getOperand(1));
  }
}
