package accela.pass;

import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.recurrence.OnDemandMemoRecurrenceMatcher;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.recurrence.Rrt2OnDemandMemoization;
import java.util.Objects;
import java.util.function.BiFunction;

/** Stable descriptor and lazy factory for the RRT2 candidate implementation. */
public final class Rrt2OnDemandMemoizationCandidate {
  public static final String ID = OnDemandMemoRecurrenceMatcher.CANDIDATE_ID;

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID,
      ID,
      "RRT2 bounded on-demand memoization",
      PassDescriptor.Stage.IR_MODULE,
      1,
      PassDescriptor.Lifecycle.CANDIDATE,
      true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_RANKED_RECURRENCE_TABULATION,
          1,
          PassDescriptor.AnchorPosition.BEFORE),
      OnDemandMemoRecurrenceMatcher.obligationIds());

  private Rrt2OnDemandMemoizationCandidate() {}

  public static PassDescriptor descriptor() {
    return DESCRIPTOR;
  }

  /** Factory installed by the central development-only candidate provider. */
  public static BiFunction<PassDescriptor, Integer, ModulePass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    return (descriptor, occurrence) ->
        new Rrt2OnDemandMemoization(instrumentation, descriptor, occurrence);
  }
}
