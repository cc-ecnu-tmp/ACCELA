package accela.pass.ir.transform.recurrence;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.Rrt2OnDemandMemoizationCandidate;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.recurrence.OnDemandMemoRecurrence;
import accela.pass.ir.analysis.recurrence.OnDemandMemoRecurrenceMatcher;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.List;
import java.util.Objects;

/** Candidate RRT2 extension for bounded, reachable-state-only memoization. */
public final class Rrt2OnDemandMemoization implements ModulePass {
  private final PassInstrumentation instrumentation;
  private final PassDescriptor descriptor;
  private final int occurrence;

  public Rrt2OnDemandMemoization(
      PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
    this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
    this.descriptor = Objects.requireNonNull(descriptor, "descriptor");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException(
          "RRT2 candidate requires enabled decision instrumentation");
    }
    if (!descriptor.equals(Rrt2OnDemandMemoizationCandidate.descriptor())) {
      throw new IllegalArgumentException("invalid RRT2 on-demand memoization descriptor");
    }
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException("invalid RRT2 candidate occurrence " + occurrence);
    }
    this.occurrence = occurrence;
  }

  @Override
  public PreservedAnalyses run(
      accela.ir.Module module,
      ModuleAnalysisManager mam,
      FunctionAnalysisManager fam) {
    boolean changed = false;
    for (Function function : List.copyOf(module.getFunctions())) {
      OnDemandMemoRecurrenceMatcher.Result match =
          OnDemandMemoRecurrenceMatcher.inspect(
              function, fam.getResult(DominatorTreeAnalysis.class, function));
      if (!match.considered()) continue;
      PassDecisionEmitter decisions = instrumentation.decisionEmitter(
          descriptor, occurrence, "function", "@" + function.getName());
      decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      if (!match.matched()) {
        decisions.rejectedLegality(match.rejectedObligationId());
        continue;
      }

      OnDemandMemoRecurrence recurrence = match.recurrence();
      Rrt2MemoizationProfitability.Decision profitability =
          Rrt2MemoizationProfitability.evaluate(module, recurrence);
      if (!profitability.profitable()) {
        decisions.rejected(
            profitability.rejection()
                    == Rrt2MemoizationProfitability.Rejection.NO_EXTERNAL_CALLER
                ? DecisionReasonCode.REJECTED_NO_BENEFIT
                : DecisionReasonCode.REJECTED_PROFITABILITY);
        continue;
      }
      if (!Rrt2MemoizationTransform.symbolsAvailable(module, recurrence)) {
        decisions.rejectedLegality(OnDemandMemoRecurrenceMatcher.NO_ABI_RUNTIME_CHANGE);
        continue;
      }
      Rrt2MemoizationTransform.apply(module, recurrence);
      decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
      changed = true;
    }
    return changed ? PreservedAnalyses.none() : PreservedAnalyses.all();
  }
}
