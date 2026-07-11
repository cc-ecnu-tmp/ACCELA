package accela.ir;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;

import org.junit.jupiter.api.Test;

final class FunctionClonerTest {
  @Test
  void remapsArgumentsInstructionsAndControlFlow() {
    Function source = new Function("source", Type.INT);
    Value argument = source.addArgument(Type.INT, "argument");
    BasicBlock entry = source.addBlock("entry");
    BasicBlock loop = source.addBlock("loop");
    BasicBlock exit = source.addBlock("exit");
    new IRBuilder(entry).createBr(loop);
    IRBuilder loopBuilder = new IRBuilder(loop);
    Instruction phi = loopBuilder.createPhi(Type.INT);
    Instruction next = loopBuilder.createAdd(phi, Constant.intConst(1));
    Instruction condition = loopBuilder.createICmp("slt", next, Constant.intConst(10));
    loopBuilder.createCondBr(condition, loop, exit);
    phi.addOperand(argument);
    phi.addOperand(entry);
    phi.addOperand(next);
    phi.addOperand(loop);
    new IRBuilder(exit).createRet(next);

    Function clone = FunctionCloner.cloneFunction(source, "source.specialized");

    assertEquals("source.specialized", clone.getName());
    assertNull(clone.getModule());
    assertNotSame(source.getArguments().get(0), clone.getArguments().get(0));
    BasicBlock clonedEntry = clone.getBlocks().get(0);
    BasicBlock clonedLoop = clone.getBlocks().get(1);
    BasicBlock clonedExit = clone.getBlocks().get(2);
    Instruction clonedPhi = clonedLoop.getInstructions().get(0);
    Instruction clonedNext = clonedLoop.getInstructions().get(1);
    Instruction clonedCondition = clonedLoop.getInstructions().get(2);
    Instruction clonedBranch = clonedLoop.getInstructions().get(3);
    assertSame(clone.getArguments().get(0), clonedPhi.getOperand(0));
    assertSame(clonedEntry, clonedPhi.getOperand(1));
    assertSame(clonedNext, clonedPhi.getOperand(2));
    assertSame(clonedLoop, clonedPhi.getOperand(3));
    assertEquals("slt", clonedCondition.getPredicate());
    assertSame(clonedLoop, clonedBranch.getOperand(1));
    assertSame(clonedExit, clonedBranch.getOperand(2));
  }
}
