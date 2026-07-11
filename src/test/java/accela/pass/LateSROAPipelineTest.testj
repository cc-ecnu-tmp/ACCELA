package accela.pass;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.instrument.PassInstrumentation;
import org.junit.jupiter.api.Test;

final class LateSROAPipelineTest {
  @Test
  void scalarizesArrayAfterIndexBecomesConstant() {
    Module module = new Module();
    Function function = new Function("late_sroa", Type.INT);
    module.addFunction(function);
    BasicBlock entry = function.addBlock("entry");
    IRBuilder builder = new IRBuilder(entry);
    Type array = Type.array(Type.INT, 2);
    Value storage = builder.createAlloca(array);
    builder.createStore(Constant.zero(array), storage);
    Value index = builder.createAdd(Constant.intConst(0), Constant.intConst(1));
    Value element = builder.createGEP(Type.INT, storage, new Value[] {index}, true);
    builder.createStore(Constant.intConst(7), element);
    Value loaded = builder.createLoad(Type.INT, element);
    builder.createRet(loaded);

    PassBuilder passBuilder = new PassBuilder();
    ModulePassManager pipeline =
        passBuilder.buildIRO0Pipeline(PassInstrumentation.noop());
    ModuleAnalysisManager mam = passBuilder.buildModuleAnalysisManager();
    FunctionAnalysisManager fam = passBuilder.buildFunctionAnalysisManager();
    pipeline.run(module, mam, fam);

    assertEquals(1, entry.getInstructions().size());
    Instruction ret = entry.getInstructions().get(0);
    Constant.Int result = assertInstanceOf(Constant.Int.class, ret.getOperand(0));
    assertEquals(7, result.value);
  }
}
