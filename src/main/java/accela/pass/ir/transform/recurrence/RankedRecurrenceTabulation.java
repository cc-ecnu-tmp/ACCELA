package accela.pass.ir.transform.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.recurrence.RankedRecurrence;
import accela.pass.ir.analysis.recurrence.RankedRecurrenceAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
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
    return run(module, fam, null, null, 0);
  }

  private static boolean run(
      accela.ir.Module module,
      FunctionAnalysisManager fam,
      PassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence) {
    boolean changed = false;
    for (Function function : List.copyOf(module.getFunctions())) {
      RankedRecurrence recurrence =
          RankedRecurrenceAnalysis.analyze(
              function, fam.getResult(DominatorTreeAnalysis.class, function));
      if (recurrence == null) continue;
      PassDecisionEmitter decisions = instrumentation == null ? null
          : instrumentation.decisionEmitter(
              descriptor, occurrence, "function", "@" + function.getName());
      if (decisions != null) decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      if (!hasExternalCall(module, function)) {
        if (decisions != null) {
          decisions.rejected(DecisionReasonCode.REJECTED_NO_BENEFIT);
        }
        continue;
      }
      Function helper = RankedRecurrenceLowering.lower(module, recurrence);
      redirectExternalCalls(module, function, helper);
      if (decisions != null) decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
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
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    /** Constructs the production pass without benchmark decision observation. */
    public Pass() {
      this.instrumentation = null;
      this.descriptor = null;
      this.occurrence = 0;
    }

    public Pass(
        PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = java.util.Objects.requireNonNull(instrumentation, "instrumentation");
      if (!instrumentation.isEnabled()) {
        throw new IllegalArgumentException(
            "instrumented ranked-recurrence pass requires enabled instrumentation");
      }
      this.descriptor = java.util.Objects.requireNonNull(descriptor, "descriptor");
      if (!descriptor.id().equals(PassRegistry.IR_RANKED_RECURRENCE_TABULATION)
          || descriptor.stage() != PassDescriptor.Stage.IR_MODULE
          || !descriptor.decisionObservable()) {
        throw new IllegalArgumentException("invalid ranked-recurrence descriptor");
      }
      if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
        throw new IllegalArgumentException("invalid pass occurrence " + occurrence);
      }
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      boolean changed = instrumentation == null
          ? RankedRecurrenceTabulation.run(module, fam)
          : RankedRecurrenceTabulation.run(module, fam, instrumentation, descriptor, occurrence);
      return changed
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
