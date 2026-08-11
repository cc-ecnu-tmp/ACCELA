package accela.pass.candidate;

import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.affine.ExtendedAffineSummarization;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Stable descriptor and lazy factory for the extended affine summarization candidate. */
public final class ExtendedAffineSummarizationCandidate {
  public static final String ID = "candidate.extended-affine-summarization";
  public static final String CANONICAL_LOOP = ID + ".canonical-loop";
  public static final String SCEV_AFFINE_STATE = ID + ".scev-affine-state";
  public static final String EXACT_TRIP_COUNT = ID + ".exact-trip-count";
  public static final String ZERO_NEGATIVE_ITERATIONS = ID + ".zero-negative-iterations";
  public static final String MODULO_I32_EQUIVALENCE = ID + ".modulo-i32-equivalence";
  public static final String SIDE_EFFECT_FREE_BODY = ID + ".side-effect-free-body";
  public static final String LIVE_OUTS = ID + ".live-outs";
  public static final String PROFITABILITY = ID + ".profitability";

  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      CANONICAL_LOOP,
      SCEV_AFFINE_STATE,
      EXACT_TRIP_COUNT,
      ZERO_NEGATIVE_ITERATIONS,
      MODULO_I32_EQUIVALENCE,
      SIDE_EFFECT_FREE_BODY,
      LIVE_OUTS,
      PROFITABILITY);

  private ExtendedAffineSummarizationCandidate() {}

  public static PassDescriptor descriptor() {
    return new PassDescriptor(
        ID,
        ID,
        "Extended affine recurrence summarization",
        PassDescriptor.Stage.IR_FUNCTION,
        1,
        PassDescriptor.Lifecycle.CANDIDATE,
        true,
        new PassDescriptor.CandidateAnchor(
            PassRegistry.IR_AFFINE_LOOP_SUMMARIZATION,
            1,
            PassDescriptor.AnchorPosition.AFTER),
        LEGALITY_OBLIGATIONS);
  }

  public static BiFunction<PassDescriptor, Integer, FunctionPass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    return (descriptor, occurrence) ->
        new ExtendedAffineSummarization.Pass(instrumentation, descriptor, occurrence);
  }
}
