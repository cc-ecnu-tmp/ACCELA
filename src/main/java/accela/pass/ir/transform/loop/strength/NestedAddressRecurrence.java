package accela.pass.ir.transform.loop.strength;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;

/** Enables the conservative nested-induction extension of loop strength reduction. */
public final class NestedAddressRecurrence {
  private NestedAddressRecurrence() {}

  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    public Pass(PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = instrumentation;
      this.descriptor = descriptor;
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      PassDecisionEmitter decision = instrumentation.decisionEmitter(
          descriptor, occurrence, "function", "@" + function.getName());
      decision.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      boolean changed = LoopStrengthReduce.runNested(function, fam);
      if (changed) {
        decision.applied(DecisionReasonCode.APPLIED_PROFITABLE);
        return PreservedAnalyses.none();
      }
      decision.rejectedLegality("candidate.nested-address-recurrence.canonical-nest");
      return PreservedAnalyses.all();
    }
  }
}
