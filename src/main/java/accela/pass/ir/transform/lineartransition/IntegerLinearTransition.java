package accela.pass.ir.transform.lineartransition;

import accela.ir.Function;
import accela.pass.IntegerLinearTransitionCandidate;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.Objects;

/** Candidate pass for small integer affine state transitions using matrix binary lifting. */
public final class IntegerLinearTransition {
  private IntegerLinearTransition() {}

  /** Runs without remark observation for focused transform tests. */
  public static boolean run(Function function, FunctionAnalysisManager analyses) {
    return run(function, analyses, null, null, 0);
  }

  private static boolean run(
      Function function,
      FunctionAnalysisManager analyses,
      PassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence) {
    LoopAnalysis.Result loops = analyses.getResult(LoopAnalysis.class, function);
    InductionVariableAnalysis.Result inductions =
        analyses.getResult(InductionVariableAnalysis.class, function);
    ScalarEvolutionAnalysis.Result scalarEvolution =
        analyses.getResult(ScalarEvolutionAnalysis.class, function);
    // Snapshot before rewriting so generated lifting loops are never reconsidered by this
    // single-occurrence candidate and independent original loops retain stable analysis objects.
    var originalLoops = new ArrayList<>(loops.loops());
    boolean changed = false;
    for (LoopAnalysis.Loop loop : originalLoops) {
      PassDecisionEmitter decisions = instrumentation == null ? null
          : instrumentation.decisionEmitter(
              descriptor, occurrence, "function", "@" + function.getName());
      if (decisions != null) decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);

      IntegerLinearTransitionMatcher.MatchResult matched =
          IntegerLinearTransitionMatcher.match(loop, inductions, scalarEvolution);
      if (matched.candidate() == null) {
        if (decisions != null) decisions.rejectedLegality(matched.rejectedObligationId());
        continue;
      }
      IntegerLinearTransitionProfitability.Result profitable =
          IntegerLinearTransitionProfitability.evaluate(matched.candidate());
      if (profitable.plan() == null) {
        if (decisions != null) decisions.rejectedLegality(profitable.rejectedObligationId());
        continue;
      }
      IntegerLinearTransitionTransform.apply(function, matched.candidate(), profitable.plan());
      if (decisions != null) decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
      changed = true;
    }
    return changed;
  }

  /** Instrumented scheduled candidate implementation. */
  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    public Pass(
        PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
      this.descriptor = Objects.requireNonNull(descriptor, "descriptor");
      if (!instrumentation.isEnabled()) {
        throw new IllegalArgumentException(
            "integer-linear-transition candidate requires enabled instrumentation");
      }
      PassDescriptor expected = IntegerLinearTransitionCandidate.descriptor();
      if (!descriptor.equals(expected)) {
        throw new IllegalArgumentException("invalid integer-linear-transition descriptor");
      }
      if (occurrence != 1) {
        throw new IllegalArgumentException(
            "integer-linear-transition candidate occurrence must be one");
      }
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager analyses) {
      return IntegerLinearTransition.run(
          function, analyses, instrumentation, descriptor, occurrence)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
