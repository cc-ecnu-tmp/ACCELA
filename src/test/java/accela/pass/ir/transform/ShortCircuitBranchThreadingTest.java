package accela.pass.ir.transform;

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
import org.junit.jupiter.api.Test;

final class ShortCircuitBranchThreadingTest {
  @Test
  void threadsConstantAndDynamicIncomingEdges() {
    Function function = new Function("or", Type.INT);
    Value first = function.addArgument(Type.I1, "first");
    Value second = function.addArgument(Type.I1, "second");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock rhs = function.addBlock("rhs");
    BasicBlock merge = function.addBlock("merge");
    BasicBlock yes = function.addBlock("yes");
    BasicBlock no = function.addBlock("no");
    new IRBuilder(entry).createCondBr(first, merge, rhs);
    IRBuilder rhsBuilder = new IRBuilder(rhs);
    Value extended = rhsBuilder.createZExt(second, Type.INT);
    rhsBuilder.createBr(merge);
    IRBuilder mergeBuilder = new IRBuilder(merge);
    Instruction phi = mergeBuilder.createPhi(Type.INT);
    phi.addOperand(Constant.intConst(1));
    phi.addOperand(entry);
    phi.addOperand(extended);
    phi.addOperand(rhs);
    Value compare = mergeBuilder.createICmp("ne", phi, Constant.intConst(0));
    mergeBuilder.createCondBr(compare, yes, no);
    new IRBuilder(yes).createRet(Constant.intConst(1));
    new IRBuilder(no).createRet(Constant.intConst(0));

    assertTrue(ShortCircuitBranchThreading.run(function));

    assertFalse(function.getBlocks().contains(merge));
    assertSame(yes, entry.getTerminator().getOperand(1));
    assertSame(rhs, entry.getTerminator().getOperand(2));
    assertEquals(Instruction.Opcode.CONDBR, rhs.getTerminator().getOpcode());
    assertSame(second, rhs.getTerminator().getOperand(0));
    assertEquals(0, function.getBlocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.ZEXT).count());
    assertEquals(0, function.getBlocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI).count());
  }
}
