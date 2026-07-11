package accela.pass.ir.transform;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.verify.IRVerifier;
import java.util.List;
import org.junit.jupiter.api.Test;

final class GlobalScalarLocalizationTest {
  @Test
  void localizesOnlyScalarGlobalsUsedExclusivelyByMain() {
    Module module = new Module();
    GlobalVariable local = new GlobalVariable(
        "local", Type.INT, Constant.intConst(3), false);
    GlobalVariable shared = new GlobalVariable(
        "shared", Type.INT, Constant.intConst(4), false);
    module.addGlobal(local);
    module.addGlobal(shared);

    Function helper = new Function("helper", Type.INT);
    module.addFunction(helper);
    new IRBuilder(helper.addBlock("entry")).createRet(
        new IRBuilder(helper.getEntryBlock()).createLoad(Type.INT, shared));

    Function main = new Function("main", Type.INT);
    module.addFunction(main);
    IRBuilder builder = new IRBuilder(main.addBlock("entry"));
    Instruction load = builder.createLoad(Type.INT, local);
    builder.createStore(builder.createAdd(load, Constant.intConst(1)), local);
    builder.createLoad(Type.INT, shared);
    builder.createRet(Constant.intConst(0));

    assertTrue(GlobalScalarLocalization.runOnModule(module));
    IRVerifier.verifyModule(module);

    List<Instruction> entry = main.getEntryBlock().getInstructions();
    assertEquals(List.of(Instruction.Opcode.ALLOCA, Instruction.Opcode.STORE,
        Instruction.Opcode.BR), entry.stream().map(Instruction::getOpcode).toList());
    Value storage = entry.get(0);
    assertSame(storage, load.getOperand(0));
    assertFalse(local.hasUses());
    assertTrue(shared.hasUses());
  }
}
