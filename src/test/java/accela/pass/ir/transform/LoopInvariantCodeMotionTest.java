package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import org.junit.jupiter.api.Test;

final class LoopInvariantCodeMotionTest {
  @Test
  void hoistsPureInvariantInstructions() {
    Function function = new Function("loop", Type.VOID);
    Value condition = function.addArgument(Type.I1, "condition");
    Value input = function.addArgument(Type.INT, "input");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock header = function.addBlock("header");
    BasicBlock body = function.addBlock("body");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    new IRBuilder(header).createCondBr(condition, body, exit);
    IRBuilder bodyBuilder = new IRBuilder(body);
    Instruction invariant = bodyBuilder.createAdd(input, Constant.intConst(7));
    bodyBuilder.createBr(header);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());

    assertTrue(LoopInvariantCodeMotion.run(function, fam));

    assertSame(entry, invariant.getParent());
    assertSame(invariant, entry.getInstructions().get(0));
  }
}
