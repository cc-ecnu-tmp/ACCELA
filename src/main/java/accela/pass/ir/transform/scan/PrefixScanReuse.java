package accela.pass.ir.transform.scan;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate pass that incrementally reuses overlapping pure i32 prefix and suffix reductions. */
public final class PrefixScanReuse {
  public static final String ID = "candidate.prefix-scan-reuse";

  public static final String REPEATED_PREFIX_DOMAIN = ID + ".repeated-prefix-domain";
  public static final String INCREMENTAL_EQUIVALENCE = ID + ".incremental-equivalence";
  public static final String INTEGER_ORDER = ID + ".integer-order";
  public static final String ALIAS_MODREF = ID + ".alias-modref";
  public static final String SIDE_EFFECT_FREE_KERNEL = ID + ".side-effect-free-kernel";
  public static final String EMPTY_DOMAIN = ID + ".empty-domain";
  public static final String BOUNDS = ID + ".bounds";
  public static final String LIVE_OUTS = ID + ".live-outs";
  public static final String PROFITABILITY = ID + ".profitability";

  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      REPEATED_PREFIX_DOMAIN,
      INCREMENTAL_EQUIVALENCE,
      INTEGER_ORDER,
      ALIAS_MODREF,
      SIDE_EFFECT_FREE_KERNEL,
      EMPTY_DOMAIN,
      BOUNDS,
      LIVE_OUTS,
      PROFITABILITY);

  private PrefixScanReuse() {}

  /** Descriptor composed into the post-implementation registry by the integration branch. */
  public static PassDescriptor descriptor() {
    return new PassDescriptor(
        ID,
        ID,
        "Incremental prefix and suffix scan reuse",
        PassDescriptor.Stage.IR_FUNCTION,
        1,
        PassDescriptor.Lifecycle.CANDIDATE,
        true,
        new PassDescriptor.CandidateAnchor(
            PassRegistry.IR_INDVAR_DOMAIN_SIMPLIFY,
            1,
            PassDescriptor.AnchorPosition.BEFORE),
        LEGALITY_OBLIGATIONS);
  }

  /** Lazy factory: constructing a disabled profile never instantiates this pass. */
  public static BiFunction<PassDescriptor, Integer, FunctionPass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    return (descriptor, occurrence) -> new Pass(instrumentation, descriptor, occurrence);
  }

  /** Direct transform entry used by focused IR tests without registering a production candidate. */
  public static boolean run(Function function, FunctionAnalysisManager fam) {
    return run(function, fam, null, null, 0);
  }

  private static boolean run(
      Function function,
      FunctionAnalysisManager fam,
      PassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence) {
    boolean changed = false;
    for (PrefixScanMatcher.Assessment assessment : PrefixScanMatcher.assess(function, fam)) {
      PassDecisionEmitter decisions = instrumentation == null ? null
          : instrumentation.decisionEmitter(
              descriptor,
              occurrence,
              "loop",
              "@" + function.getName() + ":" + assessment.target().getLabel());
      if (decisions != null) decisions.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      if (assessment.candidate() == null) {
        if (decisions != null) decisions.rejectedLegality(assessment.rejectedObligation());
        continue;
      }
      PrefixScanTransform.apply(assessment.candidate());
      if (decisions != null) decisions.applied(DecisionReasonCode.APPLIED_PROFITABLE);
      changed = true;
    }
    return changed;
  }

  /** Instrumented function-pass instance created only for an explicitly enabled profile. */
  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    public Pass(
        PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
      this.descriptor = Objects.requireNonNull(descriptor, "descriptor");
      if (!instrumentation.isEnabled()) {
        throw new IllegalArgumentException(
            "prefix-scan candidate requires enabled decision instrumentation");
      }
      if (!descriptor.equals(PrefixScanReuse.descriptor())) {
        throw new IllegalArgumentException("invalid prefix-scan candidate descriptor");
      }
      if (occurrence != 1) {
        throw new IllegalArgumentException("prefix-scan candidate occurrence must be 1");
      }
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return PrefixScanReuse.run(
              function, fam, instrumentation, descriptor, occurrence)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
