package accela.pass;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.transform.earlycse.EarlyCSE;
import org.junit.jupiter.api.Test;

final class EarlyCSETest {
  @Test
  void eliminatesRepeatedExpressions() {
    Function function = new Function("f", Type.INT);
    Value left = function.addArgument(Type.INT, "left");
    Value right = function.addArgument(Type.INT, "right");
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Instruction first = builder.createAdd(left, right);
    builder.createAdd(left, right);
    Instruction ret = builder.createRet(entry.getInstructions().get(1));

    assertTrue(EarlyCSE.runOnFunction(function));
    assertEquals(1, count(entry, Instruction.Opcode.ADD));
    assertSame(first, ret.getOperand(0));
  }

  @Test
  void forwardsAStoreThroughEquivalentGep() {
    Function function = new Function("f", Type.INT);
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Type array = Type.array(Type.INT, 4);
    Value storage = builder.createAlloca(array);
    Value firstPointer =
        builder.createGEP(array, storage, new Value[] {Constant.intConst(0)}, true);
    Value stored = Constant.intConst(7);
    builder.createStore(stored, firstPointer);
    Value secondPointer =
        builder.createGEP(array, storage, new Value[] {Constant.intConst(0)}, true);
    Value loaded = builder.createLoad(Type.INT, secondPointer);
    Instruction ret = builder.createRet(loaded);

    assertTrue(EarlyCSE.runOnFunction(function));
    assertEquals(1, count(entry, Instruction.Opcode.GEP));
    assertEquals(0, count(entry, Instruction.Opcode.LOAD));
    assertSame(stored, ret.getOperand(0));
  }

  @Test
  void storeInvalidatesAvailableLoads() {
    Function function = new Function("f", Type.INT);
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value firstPointer = builder.createAlloca(Type.INT);
    Value secondPointer = builder.createAlloca(Type.INT);
    builder.createLoad(Type.INT, firstPointer);
    builder.createStore(Constant.intConst(1), secondPointer);
    Instruction secondLoad = builder.createLoad(Type.INT, firstPointer);
    Instruction ret = builder.createRet(secondLoad);

    assertFalse(EarlyCSE.runOnFunction(function));
    assertEquals(2, count(entry, Instruction.Opcode.LOAD));
    assertSame(secondLoad, ret.getOperand(0));
  }

  @Test
  void callInvalidatesAvailableLoads() {
    Function function = new Function("f", Type.INT);
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Value pointer = builder.createAlloca(Type.INT);
    builder.createLoad(Type.INT, pointer);
    builder.createCall(new Function("unknown", Type.VOID), Type.VOID);
    Instruction secondLoad = builder.createLoad(Type.INT, pointer);
    Instruction ret = builder.createRet(secondLoad);

    assertFalse(EarlyCSE.runOnFunction(function));
    assertEquals(2, count(entry, Instruction.Opcode.LOAD));
    assertSame(secondLoad, ret.getOperand(0));
  }

  private static long count(BasicBlock block, Instruction.Opcode opcode) {
    return block.getInstructions().stream().filter(i -> i.getOpcode() == opcode).count();
  }
}
