package accela.pass;

import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.finitestate.FiniteStateAcceleration;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Stable descriptor and lazy function-pass factory for closed finite-state acceleration. */
public final class FiniteStateAccelerationCandidate {
  public static final String ID = "candidate.finite-state-acceleration";
  public static final String CONSTANT_FINITE_DOMAIN = ID + ".constant-finite-domain";
  public static final String TRANSITION_CLOSURE = ID + ".transition-closure";
  public static final String DETERMINISTIC_PURE_TRANSITION =
      ID + ".deterministic-pure-transition";
  public static final String ITERATION_DOMAIN = ID + ".iteration-domain";
  public static final String EXACT_STATE_ENCODING = ID + ".exact-state-encoding";
  public static final String MODULO_I32_EQUIVALENCE = ID + ".modulo-i32-equivalence";
  public static final String STORAGE_CODE_SIZE = ID + ".storage-code-size";
  public static final String PROFITABILITY = ID + ".profitability";

  public static final List<String> LEGALITY_OBLIGATION_IDS = List.of(
      CONSTANT_FINITE_DOMAIN,
      TRANSITION_CLOSURE,
      DETERMINISTIC_PURE_TRANSITION,
      ITERATION_DOMAIN,
      EXACT_STATE_ENCODING,
      MODULO_I32_EQUIVALENCE,
      STORAGE_CODE_SIZE,
      PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID,
      ID,
      "Closed finite-state transition acceleration",
      PassDescriptor.Stage.IR_FUNCTION,
      1,
      PassDescriptor.Lifecycle.CANDIDATE,
      true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_AFFINE_LOOP_SUMMARIZATION,
          1,
          PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATION_IDS);

  private FiniteStateAccelerationCandidate() {}

  public static PassDescriptor descriptor() {
    return DESCRIPTOR;
  }

  /** Returns a lazy factory suitable for the central executable-candidate provider. */
  public static BiFunction<PassDescriptor, Integer, FunctionPass> functionFactory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException(
          "finite-state-acceleration candidate requires enabled instrumentation");
    }
    return (descriptor, occurrence) ->
        new FiniteStateAcceleration.Pass(instrumentation, descriptor, occurrence);
  }
}
