package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.verify.IRVerifier;
import org.junit.jupiter.api.Test;

final class CFGInlinerTest {
  @Test
  void clonesBranchesAndMergesReturns() {
    Function callee = new Function("choose", Type.INT);
    Value condition = callee.addArgument(Type.I1, "condition");
    BasicBlock entry = callee.addBlock("entry");
    BasicBlock yes = callee.addBlock("yes");
    BasicBlock no = callee.addBlock("no");
    new IRBuilder(entry).createCondBr(condition, yes, no);
    new IRBuilder(yes).createRet(Constant.intConst(1));
    new IRBuilder(no).createRet(Constant.intConst(2));

    Function caller = new Function("caller", Type.INT);
    BasicBlock callerEntry = caller.addBlock("entry");
    BasicBlock merge = caller.addBlock("merge");
    IRBuilder builder = new IRBuilder(callerEntry);
    Instruction call = builder.createCall(callee, Type.INT, Constant.boolConst(true));
    Instruction sum = builder.createAdd(call, Constant.intConst(3));
    builder.createBr(merge);
    Instruction mergePhi = Instruction.createPhi(Type.INT);
    merge.addInstructionToFront(mergePhi);
    mergePhi.addOperand(sum);
    mergePhi.addOperand(callerEntry);
    new IRBuilder(merge).createRet(mergePhi);

    CFGInliner.inline(call);
    IRVerifier.verifyFunction(caller);

    assertTrue(call.getParent() == null);
    Instruction phi = (Instruction) sum.getOperand(0);
    assertEquals(Instruction.Opcode.PHI, phi.getOpcode());
    assertSame(sum.getParent(), phi.getParent());
    assertEquals(4, phi.getNumOperands());
    assertSame(sum.getParent(), mergePhi.getOperand(1));
    assertTrue(caller.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .noneMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL));
  }
}
