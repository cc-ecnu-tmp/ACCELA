package accela.pass;

import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.loop.fusion.SameDomainLoopFusion;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Stable descriptor and lazy factory for strict same-domain loop fusion. */
public final class SameDomainLoopFusionCandidate {
  public static final String ID = "candidate.same-domain-loop-fusion";
  public static final String ADJACENT_CANONICAL_LOOPS = ID + ".adjacent-canonical-loops";
  public static final String IDENTICAL_ITERATION_DOMAIN = ID + ".identical-iteration-domain";
  public static final String DEPENDENCE_ORDER = ID + ".dependence-order";
  public static final String ALIAS_MODREF = ID + ".alias-modref";
  public static final String SIDE_EFFECT_ORDER = ID + ".side-effect-order";
  public static final String TEMPORARY_LIFETIME = ID + ".temporary-lifetime";
  public static final String EXIT_LIVE_OUTS = ID + ".exit-live-outs";
  public static final String PROFITABILITY = ID + ".profitability";

  public static final List<String> LEGALITY_OBLIGATION_IDS = List.of(
      ADJACENT_CANONICAL_LOOPS,
      IDENTICAL_ITERATION_DOMAIN,
      DEPENDENCE_ORDER,
      ALIAS_MODREF,
      SIDE_EFFECT_ORDER,
      TEMPORARY_LIFETIME,
      EXIT_LIVE_OUTS,
      PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID,
      ID,
      "Strict same-domain loop fusion and temporary contraction",
      PassDescriptor.Stage.IR_FUNCTION,
      1,
      PassDescriptor.Lifecycle.CANDIDATE,
      true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_INDVAR_DOMAIN_SIMPLIFY,
          1,
          PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATION_IDS);

  private SameDomainLoopFusionCandidate() {}

  public static PassDescriptor descriptor() {
    return DESCRIPTOR;
  }

  /** Returns the lazy factory installed only by the development candidate provider. */
  public static BiFunction<PassDescriptor, Integer, FunctionPass> functionFactory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException(
          "same-domain-loop-fusion candidate requires enabled instrumentation");
    }
    return (descriptor, occurrence) ->
        new SameDomainLoopFusion.Pass(instrumentation, descriptor, occurrence);
  }
}
