package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.verify.IRVerifier;
import org.junit.jupiter.api.Test;

final class LoopInvariantCSETest {
  @Test
  void hoistsRepeatedScalarGlobalLoadsPastUnrelatedStores() {
    Function function = new Function("read_global", Type.VOID);
    Value output = function.addArgument(Type.PTR, "output");
    Value condition = function.addArgument(Type.I1, "condition");
    GlobalVariable global = new GlobalVariable(
        "limit", Type.INT, Constant.intConst(0), false);
    BasicBlock entry = function.addBlock("entry");
    BasicBlock header = function.addBlock("header");
    BasicBlock body = function.addBlock("body");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    IRBuilder headerBuilder = new IRBuilder(header);
    Instruction headerLoad = headerBuilder.createLoad(Type.INT, global);
    headerBuilder.createCondBr(condition, body, exit);
    IRBuilder bodyBuilder = new IRBuilder(body);
    Instruction bodyLoad = bodyBuilder.createLoad(Type.INT, global);
    Instruction store = bodyBuilder.createStore(bodyLoad, output);
    bodyBuilder.createBr(header);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = analysisManager();

    assertTrue(LoopInvariantCSE.run(function, fam));
    IRVerifier.verifyFunction(function);

    assertSame(entry, headerLoad.getParent());
    assertSame(headerLoad, store.getOperand(0));
    assertTrue(bodyLoad.getParent() == null);
  }

  @Test
  void hoistsRepeatedInvariantDivisionFromLoopHeader() {
    Function function = new Function("half", Type.VOID);
    Value value = function.addArgument(Type.INT, "value");
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock header = function.addBlock("header");
    BasicBlock body = function.addBlock("body");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    IRBuilder headerBuilder = new IRBuilder(header);
    Instruction headerHalf = headerBuilder.createSDiv(value, Constant.intConst(2));
    headerBuilder.createCondBr(condition, body, exit);
    IRBuilder bodyBuilder = new IRBuilder(body);
    Instruction bodyHalf = bodyBuilder.createSDiv(value, Constant.intConst(2));
    Instruction use = bodyBuilder.createAdd(bodyHalf, Constant.intConst(1));
    bodyBuilder.createBr(header);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = analysisManager();

    assertTrue(LoopInvariantCSE.run(function, fam));
    IRVerifier.verifyFunction(function);

    assertSame(entry, headerHalf.getParent());
    assertSame(headerHalf, use.getOperand(0));
    assertTrue(bodyHalf.getParent() == null);
  }

  private static FunctionAnalysisManager analysisManager() {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    return fam;
  }
}
