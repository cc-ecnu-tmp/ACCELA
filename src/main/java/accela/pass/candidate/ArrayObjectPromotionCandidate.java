package accela.pass.candidate;

import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.sroa.ArrayObjectPromotion;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate descriptor for bounded local-array promotion and constant aggregate folding. */
public final class ArrayObjectPromotionCandidate {
  public static final String ID = "candidate.array-object-promotion";
  public static final String NON_ESCAPING = ID + ".non-escaping";
  public static final String CONSTANT_INDEX = ID + ".constant-index";
  public static final String ELEMENT_BUDGET = ID + ".element-budget";
  public static final String IMMUTABLE_GLOBAL = ID + ".immutable-global";
  public static final String PROFITABILITY = ID + ".profitability";
  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      NON_ESCAPING, CONSTANT_INDEX, ELEMENT_BUDGET, IMMUTABLE_GLOBAL, PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID, ID, "Bounded SysY array-object promotion", PassDescriptor.Stage.IR_FUNCTION, 1,
      PassDescriptor.Lifecycle.CANDIDATE, true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_SROA, 1, PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATIONS);

  private ArrayObjectPromotionCandidate() {}

  public static PassDescriptor descriptor() { return DESCRIPTOR; }

  public static BiFunction<PassDescriptor, Integer, FunctionPass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException("array-object-promotion candidate requires instrumentation");
    }
    return (descriptor, occurrence) ->
        new ArrayObjectPromotion.Pass(instrumentation, descriptor, occurrence);
  }
}
