package accela.pass;

import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.lineartransition.IntegerLinearTransition;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Stable descriptor and lazy function-pass factory for integer affine state transitions. */
public final class IntegerLinearTransitionCandidate {
  public static final String ID = "candidate.integer-linear-transition";
  public static final String SMALL_FIXED_DIMENSION = ID + ".small-fixed-dimension";
  public static final String AFFINE_TRANSITION = ID + ".affine-transition";
  public static final String EXACT_TRIP_COUNT = ID + ".exact-trip-count";
  public static final String ZERO_NEGATIVE_ITERATIONS = ID + ".zero-negative-iterations";
  public static final String MODULO_I32_EQUIVALENCE = ID + ".modulo-i32-equivalence";
  public static final String INTEGER_ONLY = ID + ".integer-only";
  public static final String SIDE_EFFECT_FREE_BODY = ID + ".side-effect-free-body";
  public static final String LIVE_OUTS = ID + ".live-outs";
  public static final String PROFITABILITY = ID + ".profitability";

  public static final List<String> LEGALITY_OBLIGATION_IDS = List.of(
      SMALL_FIXED_DIMENSION,
      AFFINE_TRANSITION,
      EXACT_TRIP_COUNT,
      ZERO_NEGATIVE_ITERATIONS,
      MODULO_I32_EQUIVALENCE,
      INTEGER_ONLY,
      SIDE_EFFECT_FREE_BODY,
      LIVE_OUTS,
      PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID,
      ID,
      "Integer affine state-transition acceleration",
      PassDescriptor.Stage.IR_FUNCTION,
      1,
      PassDescriptor.Lifecycle.CANDIDATE,
      true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_AFFINE_LOOP_SUMMARIZATION,
          1,
          PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATION_IDS);

  private IntegerLinearTransitionCandidate() {}

  public static PassDescriptor descriptor() {
    return DESCRIPTOR;
  }

  /** Returns a lazy factory suitable for the central candidate provider. */
  public static BiFunction<PassDescriptor, Integer, FunctionPass> functionFactory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException(
          "integer-linear-transition candidate requires enabled instrumentation");
    }
    return (descriptor, occurrence) ->
        new IntegerLinearTransition.Pass(instrumentation, descriptor, occurrence);
  }
}
