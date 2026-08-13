package accela.pass.ir.transform.loop.fusion;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.SameDomainLoopFusionCandidate;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.LinkedHashSet;
import java.util.Objects;
import java.util.Set;

/** Candidate pass for strict adjacent same-domain loop fusion. */
public final class SameDomainLoopFusion {
  private SameDomainLoopFusion() {}

  /** Runs without decision remarks for focused transform tests. */
  public static boolean run(Function function, FunctionAnalysisManager analyses) {
    return run(function, analyses, null, null, 0);
  }

  private static boolean run(
      Function function,
      FunctionAnalysisManager analyses,
      PassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence) {
    Set<PairIdentity> attempted = new LinkedHashSet<>();
    boolean changed = false;
    while (true) {
      boolean applied = false;
      InductionVariableAnalysis.Result inductions =
          analyses.getResult(InductionVariableAnalysis.class, function);
      for (SameDomainLoopFusionMatcher.AdjacentPair pair :
          SameDomainLoopFusionMatcher.findAdjacentPairs(function, analyses)) {
        PairIdentity identity = new PairIdentity(pair.first().header(), pair.second().header());
        if (!attempted.add(identity)) continue;
        PassDecisionEmitter decisions = instrumentation == null ? null
            : instrumentation.decisionEmitter(
                descriptor,
                occurrence,
                "loop-pair",
                "@" + function.getName() + ":" + pair.first().header().getLabel()
                    + "->" + pair.second().header().getLabel());
        if (decisions != null) decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
        SameDomainLoopFusionMatcher.MatchResult matched =
            SameDomainLoopFusionMatcher.match(function, pair, inductions);
        if (matched.candidate() == null) {
          if (decisions != null) decisions.rejectedLegality(matched.rejectedObligationId());
          continue;
        }
        SameDomainLoopFusionProfitability.Result profitability =
            SameDomainLoopFusionProfitability.evaluate(matched.candidate());
        if (profitability.plan() == null) {
          if (decisions != null) {
            decisions.rejectedLegality(profitability.rejectedObligationId());
          }
          continue;
        }
        SameDomainLoopFusionTransform.apply(matched.candidate(), profitability.plan());
        if (decisions != null) decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
        changed = true;
        applied = true;
        analyses.invalidate(function, PreservedAnalyses.none());
        // A neighboring fusion can make a formerly live temporary fully contractible, so every
        // surviving pair must be reconsidered against the new whole-function lifetime proof.
        attempted.clear();
        break;
      }
      if (!applied) return changed;
    }
  }

  private record PairIdentity(Object first, Object second) {}

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
            "same-domain-loop-fusion candidate requires enabled instrumentation");
      }
      if (!descriptor.equals(SameDomainLoopFusionCandidate.descriptor())) {
        throw new IllegalArgumentException("invalid same-domain-loop-fusion descriptor");
      }
      if (occurrence != 1) {
        throw new IllegalArgumentException(
            "same-domain-loop-fusion candidate occurrence must be one");
      }
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager analyses) {
      return SameDomainLoopFusion.run(
          function, analyses, instrumentation, descriptor, occurrence)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
