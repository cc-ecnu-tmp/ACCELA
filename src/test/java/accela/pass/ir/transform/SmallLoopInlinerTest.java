package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.verify.IRVerifier;
import org.junit.jupiter.api.Test;

final class SmallLoopInlinerTest {
  @Test
  void inlinesUniquelyCalledLeafLoop() {
    Module module = new Module();
    Function loop = new Function("loop", Type.INT);
    module.addFunction(loop);
    Value condition = loop.addArgument(Type.I1, "condition");
    BasicBlock entry = loop.addBlock("entry");
    BasicBlock header = loop.addBlock("header");
    BasicBlock body = loop.addBlock("body");
    BasicBlock exit = loop.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    Instruction index = Instruction.createPhi(Type.INT);
    header.addInstructionToFront(index);
    index.addOperand(Constant.intConst(0));
    index.addOperand(entry);
    new IRBuilder(header).createCondBr(condition, body, exit);
    IRBuilder bodyBuilder = new IRBuilder(body);
    Instruction next = bodyBuilder.createAdd(index, Constant.intConst(1));
    bodyBuilder.createBr(header);
    index.addOperand(next);
    index.addOperand(body);
    new IRBuilder(exit).createRet(index);

    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    IRBuilder mainBuilder = new IRBuilder(main.addBlock("entry"));
    Value call = mainBuilder.createCall(loop, Type.INT, Constant.boolConst(false));
    mainBuilder.createRet(call);

    assertTrue(SmallLoopInliner.runOnModule(module));
    IRVerifier.verifyModule(module);
    assertTrue(main.getBlocks().size() > 1);
    assertTrue(main.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .noneMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL));
  }
}
