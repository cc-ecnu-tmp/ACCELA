package accela.pass.ir.analysis;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import org.junit.jupiter.api.Test;

final class InductionVariableAnalysisTest {
  @Test
  void recognizesCanonicalAddRecurrences() {
    Function function = new Function("loop", Type.VOID);
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock header = function.addBlock("header");
    BasicBlock body = function.addBlock("body");
    BasicBlock exit = function.addBlock("exit");
    new IRBuilder(entry).createBr(header);
    Instruction phi = Instruction.createPhi(Type.INT);
    header.addInstructionToFront(phi);
    phi.addOperand(Constant.intConst(3));
    phi.addOperand(entry);
    new IRBuilder(header).createCondBr(condition, body, exit);
    IRBuilder bodyBuilder = new IRBuilder(body);
    Instruction next = bodyBuilder.createAdd(phi, Constant.intConst(2));
    bodyBuilder.createBr(header);
    phi.addOperand(next);
    phi.addOperand(body);
    new IRBuilder(exit).createRetVoid();
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());

    var induction = fam.getResult(
        InductionVariableAnalysis.class, function).inductions().get(0);

    assertSame(phi, induction.phi());
    assertSame(next, induction.next());
    assertEquals(3, ((Constant.Int) induction.start()).value);
    assertEquals(2, induction.step());
  }
}
