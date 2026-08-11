package accela.pass.ir.transform.finitestate;

import accela.ir.Function;
import accela.pass.FiniteStateAccelerationCandidate;
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

/** Candidate pass for closed small-domain integer transitions using binary lifting. */
public final class FiniteStateAcceleration {
  private FiniteStateAcceleration() {}

  /** Runs without remark observation for focused transform tests. */
  public static boolean run(Function function, FunctionAnalysisManager analyses) {
    return run(function, analyses, null, null, 0, new ResourceBudget());
  }

  private static boolean run(
      Function function,
      FunctionAnalysisManager analyses,
      PassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence,
      ResourceBudget resourceBudget) {
    LoopAnalysis.Result loops = analyses.getResult(LoopAnalysis.class, function);
    InductionVariableAnalysis.Result inductions =
        analyses.getResult(InductionVariableAnalysis.class, function);
    ScalarEvolutionAnalysis.Result scalarEvolution =
        analyses.getResult(ScalarEvolutionAnalysis.class, function);
    var originalLoops = new ArrayList<>(loops.loops());
    boolean changed = false;
    for (LoopAnalysis.Loop loop : originalLoops) {
      PassDecisionEmitter decisions = instrumentation == null ? null
          : instrumentation.decisionEmitter(
              descriptor, occurrence, "function", "@" + function.getName());
      if (decisions != null) decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);

      FiniteStateAccelerationMatcher.MatchResult matched =
          FiniteStateAccelerationMatcher.match(loop, inductions, scalarEvolution);
      if (matched.candidate() == null) {
        if (decisions != null) decisions.rejectedLegality(matched.rejectedObligationId());
        continue;
      }
      FiniteStateAccelerationProfitability.Result profitable =
          FiniteStateAccelerationProfitability.evaluate(
              matched.candidate(), resourceBudget.allocatedTableBytes);
      if (profitable.plan() == null) {
        if (decisions != null) decisions.rejectedLegality(profitable.rejectedObligationId());
        continue;
      }
      FiniteStateAccelerationTransform.apply(function, matched.candidate(), profitable.plan());
      resourceBudget.allocatedTableBytes += profitable.plan().tableBytes();
      if (decisions != null) decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
      changed = true;
    }
    return changed;
  }

  private static final class ResourceBudget {
    int allocatedTableBytes;
  }

  /** Instrumented scheduled candidate implementation. */
  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;
    private final ResourceBudget resourceBudget = new ResourceBudget();

    public Pass(
        PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
      this.descriptor = Objects.requireNonNull(descriptor, "descriptor");
      if (!instrumentation.isEnabled()) {
        throw new IllegalArgumentException(
            "finite-state-acceleration candidate requires enabled instrumentation");
      }
      if (!descriptor.equals(FiniteStateAccelerationCandidate.descriptor())) {
        throw new IllegalArgumentException("invalid finite-state-acceleration descriptor");
      }
      if (occurrence != 1) {
        throw new IllegalArgumentException(
            "finite-state-acceleration candidate occurrence must be one");
      }
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager analyses) {
      return FiniteStateAcceleration.run(
          function, analyses, instrumentation, descriptor, occurrence, resourceBudget)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
