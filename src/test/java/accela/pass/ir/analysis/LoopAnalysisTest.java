package accela.pass.ir.analysis;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import org.junit.jupiter.api.Test;

final class LoopAnalysisTest {
  @Test
  void findsNaturalLoopStructure() {
    Function function = new Function("loop", Type.VOID);
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock header = function.addBlock("header");
    BasicBlock body = function.addBlock("body");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    new IRBuilder(header).createCondBr(condition, body, exit);
    new IRBuilder(body).createBr(header);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());

    LoopAnalysis.Loop loop = fam.getResult(LoopAnalysis.class, function).loops().get(0);

    assertSame(header, loop.header());
    assertSame(body, loop.latch());
    assertSame(entry, loop.preheader());
    assertEquals(2, loop.blocks().size());
    assertTrue(loop.blocks().containsAll(java.util.List.of(header, body)));
  }
}
