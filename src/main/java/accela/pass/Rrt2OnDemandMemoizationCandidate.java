package accela.pass;

import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.recurrence.OnDemandMemoRecurrenceMatcher;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.recurrence.Rrt2OnDemandMemoization;
import java.util.ArrayList;
import java.util.Map;
import java.util.Objects;
import java.util.function.BiFunction;

/** Composable descriptor and lazy factory for the isolated RRT2 candidate implementation. */
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

  /** Registry used by this candidate's isolated development compiler and integration tests. */
  public static PassRegistry combinedRegistry() {
    ArrayList<PassDescriptor> descriptors = new ArrayList<>(PassRegistry.standard().all());
    descriptors.add(DESCRIPTOR);
    return PassRegistry.of(descriptors);
  }

  /** Factory that an integration registry can compose with factories from other candidates. */
  public static BiFunction<PassDescriptor, Integer, ModulePass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    return (descriptor, occurrence) ->
        new Rrt2OnDemandMemoization(instrumentation, descriptor, occurrence);
  }

  /** Builds the real production pipeline with this candidate available at its declared anchor. */
  public static PassBuilder passBuilder(PassInstrumentation instrumentation) {
    return passBuilder(instrumentation, factory(instrumentation));
  }

  static PassBuilder passBuilder(
      PassInstrumentation instrumentation,
      BiFunction<PassDescriptor, Integer, ModulePass> candidateFactory) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    Objects.requireNonNull(candidateFactory, "candidateFactory");
    PassBuilder.CandidatePassProvider provider = new PassBuilder.CandidatePassProvider(
        Map.of(), Map.of(ID, candidateFactory));
    return new PassBuilder(provider);
  }
}
