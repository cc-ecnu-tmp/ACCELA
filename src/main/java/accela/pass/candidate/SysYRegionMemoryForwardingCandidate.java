package accela.pass.candidate;

import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.region.SysYRegionMemoryForwarding;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate descriptor for SysY object/region-aware load forwarding. */
public final class SysYRegionMemoryForwardingCandidate {
  public static final String ID = "candidate.sysy-region-memory-forwarding";
  public static final String OBJECT_PROVENANCE = ID + ".object-provenance";
  public static final String SHAPE_STRIDE = ID + ".shape-stride";
  public static final String FUNCTION_EFFECTS = ID + ".function-effects";
  public static final String REGION_DISJOINTNESS = ID + ".region-disjointness";
  public static final String PROFITABILITY = ID + ".profitability";
  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      OBJECT_PROVENANCE, SHAPE_STRIDE, FUNCTION_EFFECTS, REGION_DISJOINTNESS, PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID, ID, "SysY array-region memory forwarding", PassDescriptor.Stage.IR_FUNCTION, 1,
      PassDescriptor.Lifecycle.CANDIDATE, true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_GVN, 1, PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATIONS);

  private SysYRegionMemoryForwardingCandidate() {}

  public static PassDescriptor descriptor() { return DESCRIPTOR; }

  public static BiFunction<PassDescriptor, Integer, FunctionPass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException("region-memory candidate requires instrumentation");
    }
    return (descriptor, occurrence) ->
        new SysYRegionMemoryForwarding.Pass(instrumentation, descriptor, occurrence);
  }
}
