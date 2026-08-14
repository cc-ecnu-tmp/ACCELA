package accela.pass.candidate;

import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.loop.strength.NestedAddressRecurrence;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate descriptor for nested row/element pointer recurrences. */
public final class NestedAddressRecurrenceCandidate {
  public static final String ID = "candidate.nested-address-recurrence";
  public static final String CANONICAL_NEST = ID + ".canonical-nest";
  public static final String ROW_MAJOR_SHAPE = ID + ".row-major-shape";
  public static final String POSITIVE_STEP = ID + ".positive-step";
  public static final String LIVE_OUTS = ID + ".live-outs";
  public static final String PROFITABILITY = ID + ".profitability";
  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      CANONICAL_NEST, ROW_MAJOR_SHAPE, POSITIVE_STEP, LIVE_OUTS, PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID, ID, "Nested SysY array address recurrences", PassDescriptor.Stage.IR_FUNCTION, 1,
      PassDescriptor.Lifecycle.CANDIDATE, true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_LOOP_STRENGTH_REDUCE, 1, PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATIONS);

  private NestedAddressRecurrenceCandidate() {}

  public static PassDescriptor descriptor() { return DESCRIPTOR; }

  public static BiFunction<PassDescriptor, Integer, FunctionPass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException("nested-address candidate requires instrumentation");
    }
    return (descriptor, occurrence) ->
        new NestedAddressRecurrence.Pass(instrumentation, descriptor, occurrence);
  }
}
