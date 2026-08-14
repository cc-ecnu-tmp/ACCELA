package accela.pass.candidate;

import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.ir.ModulePass;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.specialize.FunctionSpecialization;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate descriptor for bounded direct-call specialization. */
public final class FunctionSpecializationCandidate {
  public static final String ID = "candidate.function-specialization";
  public static final String DIRECT_NON_RECURSIVE = ID + ".direct-non-recursive";
  public static final String CONSTANT_ARGUMENT = ID + ".constant-argument";
  public static final String EFFECT_SUMMARY = ID + ".effect-summary";
  public static final String CODE_SIZE_BUDGET = ID + ".code-size-budget";
  public static final String PROFITABILITY = ID + ".profitability";
  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      DIRECT_NON_RECURSIVE, CONSTANT_ARGUMENT, EFFECT_SUMMARY, CODE_SIZE_BUDGET, PROFITABILITY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID, ID, "Bounded SysY function specialization", PassDescriptor.Stage.IR_MODULE, 1,
      PassDescriptor.Lifecycle.CANDIDATE, true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.IR_INLINER, 1, PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATIONS);

  private FunctionSpecializationCandidate() {}

  public static PassDescriptor descriptor() { return DESCRIPTOR; }

  public static BiFunction<PassDescriptor, Integer, ModulePass> factory(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException("function-specialization candidate requires instrumentation");
    }
    return (descriptor, occurrence) ->
        new FunctionSpecialization.Pass(instrumentation, descriptor, occurrence);
  }
}
