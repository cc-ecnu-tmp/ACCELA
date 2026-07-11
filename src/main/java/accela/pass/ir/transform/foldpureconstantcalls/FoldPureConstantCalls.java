package accela.pass.ir.transform.foldpureconstantcalls;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.ArrayList;
import java.util.List;

/** Folds all-constant calls to pure straight-line functions. */
public final class FoldPureConstantCalls {
  private FoldPureConstantCalls() {}

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  static boolean runOnModule(accela.ir.Module module) {
    boolean changed = false;
    for (Function function : module.getFunctions()) {
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
          if (foldCall(module, instruction)) changed = true;
        }
      }
    }
    return changed;
  }

  private static boolean foldCall(accela.ir.Module module, Instruction call) {
    if (call.getOpcode() != Instruction.Opcode.CALL
        || call.getCallee() == null
        || !module.getFunctions().contains(call.getCallee())) {
      return false;
    }
    List<Constant.Int> arguments = new ArrayList<>();
    for (int i = 0; i < call.getNumOperands(); i++) {
      if (!(call.getOperand(i) instanceof Constant.Int constant)) return false;
      arguments.add(constant);
    }
    Constant.Int result = StraightLineConstantEvaluator.evaluate(call.getCallee(), arguments);
    if (result == null) return false;
    call.replaceAllUsesWith(result);
    call.eraseFromParent();
    return true;
  }
}
