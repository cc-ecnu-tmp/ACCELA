package accela.pass.candidate;

import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.loop.tiling.CostModelLoopTiling;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate descriptor for conservative RV64GC loop tiling. */
public final class CostModelLoopTilingCandidate {
  public static final String ID = "candidate.cost-model-loop-tiling";
  public static final String PERFECT_AFFINE_NEST = ID + ".perfect-affine-nest";
  public static final String DEPENDENCE_SAFETY = ID + ".dependence-safety";
  public static final String TARGET_COST = ID + ".target-cost";
  public static final String SCALAR_REMAINDER = ID + ".scalar-remainder";
  public static final String PROFITABILITY = ID + ".profitability";
  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      PERFECT_AFFINE_NEST, DEPENDENCE_SAFETY, TARGET_COST, SCALAR_REMAINDER, PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID, ID, "RV64GC cost-model loop tiling", PassDescriptor.Stage.IR_FUNCTION, 1,
      PassDescriptor.Lifecycle.CANDIDATE, true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_LOOP_INTERCHANGE, 1, PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATIONS);

  private CostModelLoopTilingCandidate() {}

  public static PassDescriptor descriptor() { return DESCRIPTOR; }

  public static BiFunction<PassDescriptor, Integer, FunctionPass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException("loop-tiling candidate requires instrumentation");
    }
    return (descriptor, occurrence) ->
        new CostModelLoopTiling.Pass(instrumentation, descriptor, occurrence);
  }
}
