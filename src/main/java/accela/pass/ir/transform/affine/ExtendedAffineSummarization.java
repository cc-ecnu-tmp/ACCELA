package accela.pass.ir.transform.affine;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.candidate.ExtendedAffineSummarizationCandidate;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/** Candidate pass that extends the shared SCEV/Affine path to degree-two i32 recurrences. */
public final class ExtendedAffineSummarization {
  private ExtendedAffineSummarization() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    return run(function, fam, null, null, 0);
  }

  private static boolean run(
      Function function,
      FunctionAnalysisManager fam,
      PassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence) {
    ScalarEvolutionAnalysis.Result scalarEvolution =
        fam.getResult(ScalarEvolutionAnalysis.class, function);
    List<EvaluatedLoop> evaluated = new ArrayList<>();
    for (LoopAnalysis.Loop loop : scalarEvolution.getLoopInfo().loops()) {
      ExtendedAffineMatcher.Inspection inspection =
          ExtendedAffineMatcher.inspect(loop, scalarEvolution);
      ExtendedAffineProfitability.Assessment profitability = inspection.matched()
          ? ExtendedAffineProfitability.assess(inspection.plan()) : null;
      evaluated.add(new EvaluatedLoop(loop, inspection, profitability));
    }

    boolean changed = false;
    for (EvaluatedLoop item : evaluated) {
      PassDecisionEmitter decisions = instrumentation == null ? null
          : instrumentation.decisionEmitter(
              descriptor,
              occurrence,
              "loop",
              "@" + function.getName() + ":%" + item.loop().header().getLabel());
      if (decisions != null) decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      if (!item.inspection().matched()) {
        if (decisions != null) {
          decisions.rejectedLegality(obligation(item.inspection().failure()));
        }
        continue;
      }
      if (!item.profitability().profitable()) {
        if (decisions != null) {
          decisions.rejectedLegality(ExtendedAffineSummarizationCandidate.PROFITABILITY);
        }
        continue;
      }
      ExtendedAffineTransform.apply(
          function, item.inspection().plan(), item.profitability(), scalarEvolution);
      if (decisions != null) decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
      changed = true;
    }
    return changed;
  }

  private static String obligation(ExtendedAffineMatcher.Failure failure) {
    return switch (failure) {
      case CANONICAL_LOOP -> ExtendedAffineSummarizationCandidate.CANONICAL_LOOP;
      case SCEV_AFFINE_STATE -> ExtendedAffineSummarizationCandidate.SCEV_AFFINE_STATE;
      case EXACT_TRIP_COUNT -> ExtendedAffineSummarizationCandidate.EXACT_TRIP_COUNT;
      case ZERO_NEGATIVE_ITERATIONS ->
          ExtendedAffineSummarizationCandidate.ZERO_NEGATIVE_ITERATIONS;
      case MODULO_I32_EQUIVALENCE ->
          ExtendedAffineSummarizationCandidate.MODULO_I32_EQUIVALENCE;
      case SIDE_EFFECT_FREE_BODY ->
          ExtendedAffineSummarizationCandidate.SIDE_EFFECT_FREE_BODY;
      case LIVE_OUTS -> ExtendedAffineSummarizationCandidate.LIVE_OUTS;
    };
  }

  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    public Pass(
        PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
      if (!instrumentation.isEnabled()) {
        throw new IllegalArgumentException(
            "extended affine candidate requires enabled decision instrumentation");
      }
      this.descriptor = Objects.requireNonNull(descriptor, "descriptor");
      PassDescriptor expected = ExtendedAffineSummarizationCandidate.descriptor();
      if (!descriptor.equals(expected)) {
        throw new IllegalArgumentException("invalid extended affine candidate descriptor");
      }
      if (occurrence != 1) {
        throw new IllegalArgumentException("extended affine candidate occurrence must be one");
      }
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return ExtendedAffineSummarization.run(
              function, fam, instrumentation, descriptor, occurrence)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }

  private record EvaluatedLoop(
      LoopAnalysis.Loop loop,
      ExtendedAffineMatcher.Inspection inspection,
      ExtendedAffineProfitability.Assessment profitability) {}
}
