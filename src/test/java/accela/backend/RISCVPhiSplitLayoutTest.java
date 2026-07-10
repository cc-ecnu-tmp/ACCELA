package accela.backend;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import org.junit.jupiter.api.Test;

final class RISCVPhiSplitLayoutTest {
  @Test
  void placesCriticalEdgeCopiesBeforeTheirSuccessor() {
    Module module = new Module();
    Function function = new Function("phi_layout", Type.INT);
    module.addFunction(function);
    Value condition = function.addArgument(Type.I1, "condition");
    BasicBlock entry = function.addBlock("entry");
    BasicBlock other = function.addBlock("other");
    BasicBlock merge = function.addBlock("merge");
    new IRBuilder(entry).createCondBr(condition, merge, other);
    new IRBuilder(other).createBr(merge);
    Instruction phi = Instruction.createPhi(Type.INT);
    merge.addInstructionToFront(phi);
    phi.addOperand(Constant.intConst(1));
    phi.addOperand(entry);
    phi.addOperand(Constant.intConst(2));
    phi.addOperand(other);
    new IRBuilder(merge).createRet(phi);

    String assembly = new BackendCompiler().compileToAssembly(module);
    int split = assembly.indexOf(".L_phi_layout_entry_to_merge_phi_0:");
    int successor = assembly.indexOf(".L_phi_layout_merge:");

    assertTrue(split >= 0 && successor > split);
    assertFalse(assembly.substring(split, successor).contains("  j "));
  }
}
