package accela.pass.ir.transform.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.recurrence.RankedRecurrence;
import accela.pass.ir.analysis.recurrence.RankedRecurrenceAnalysis;
import java.util.List;

/** Converts pure finite recursion with a strictly decreasing rank into bottom-up table loops. */
public final class RankedRecurrenceTabulation {
  private RankedRecurrenceTabulation() {}

  static boolean run(accela.ir.Module module) {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    return run(module, fam);
  }

  private static boolean run(accela.ir.Module module, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (Function function : List.copyOf(module.getFunctions())) {
      RankedRecurrence recurrence =
          RankedRecurrenceAnalysis.analyze(
              function, fam.getResult(DominatorTreeAnalysis.class, function));
      if (recurrence == null || !hasExternalCall(module, function)) continue;
      Function helper = RankedRecurrenceLowering.lower(module, recurrence);
      redirectExternalCalls(module, function, helper);
      changed = true;
    }
    return changed;
  }

  private static boolean hasExternalCall(accela.ir.Module module, Function target) {
    return module.getFunctions().stream()
        .filter(function -> function != target)
        .flatMap(function -> function.getBlocks().stream())
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> isCallTo(instruction, target));
  }

  private static void redirectExternalCalls(
      accela.ir.Module module, Function original, Function helper) {
    for (Function function : module.getFunctions()) {
      if (function == original || function == helper) continue;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (isCallTo(instruction, original)) instruction.setCallee(helper);
        }
      }
    }
  }

  private static boolean isCallTo(Instruction instruction, Function target) {
    return instruction.getOpcode() == Instruction.Opcode.CALL
        && instruction.getCallee() == target;
  }

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return RankedRecurrenceTabulation.run(module, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
