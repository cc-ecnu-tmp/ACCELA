package accela.cost;

import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineVerifier;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import java.util.Map;
import java.util.Objects;
import java.util.function.Predicate;

/** Transactional R1 scheduler for Machine IR transforms. */
public final class MachineCandidateScheduler {
  private final TargetProfile profile;
  private final RegisterAllocator allocator;
  private final RISCVTarget target;
  private final MachineCostModel costModel;
  private final DecisionTraceSink trace;
  private int expansions;

  public MachineCandidateScheduler(
      TargetProfile profile,
      RegisterAllocator allocator,
      RISCVTarget target,
      DecisionTraceSink trace) {
    this.profile = Objects.requireNonNull(profile, "profile");
    this.allocator = Objects.requireNonNull(allocator, "allocator");
    this.target = Objects.requireNonNull(target, "target");
    this.trace = Objects.requireNonNull(trace, "trace");
    costModel = new MachineCostModel(profile, allocator, target);
  }

  public boolean apply(
      String candidateId,
      LegalityResult legality,
      MachineFunction function,
      Predicate<MachineFunction> transform) {
    Objects.requireNonNull(legality, "legality");
    if (expansions >= profile.scheduler().maxFunctionExpansions()) {
      trace.accept(new DecisionTraceSink.Decision(profile.id(), profile.evidenceLevel().name().toLowerCase(),
          candidateId, "machine-function", function.getName(), "rejected", "budget_exhausted",
          "not_evaluated", legality.obligation(), Map.of(), null, null, null,
          expansions, profile.scheduler().maxFunctionExpansions()));
      return false;
    }
    if (!legality.mayApply()) {
      trace.accept(new DecisionTraceSink.Decision(profile.id(), profile.evidenceLevel().name().toLowerCase(),
          candidateId, "machine-function", function.getName(), "rejected", legality.detail(),
          legality.status().name().toLowerCase(), legality.obligation(), Map.of(), null, null, null,
          expansions, profile.scheduler().maxFunctionExpansions()));
      return false;
    }
    expansions++;
    MachineVerifier.verify(function);
    MachineFunction candidate = function.deepCopy();
    boolean changed = transform.test(candidate);
    if (!changed) {
      trace.accept(new DecisionTraceSink.Decision(profile.id(), profile.evidenceLevel().name().toLowerCase(),
          candidateId, "machine-function", function.getName(), "rejected", "no_change",
          "not_applicable", legality.obligation(), Map.of(), null, null, null,
          expansions, profile.scheduler().maxFunctionExpansions()));
      return false;
    }
    MachineVerifier.verify(candidate);
    if (!profile.calibrated() || !profile.scheduler().enabled()) {
      function.replaceWith(candidate);
      trace.accept(new DecisionTraceSink.Decision(profile.id(), profile.evidenceLevel().name().toLowerCase(),
          candidateId, "machine-function", function.getName(), "applied",
          "uncalibrated_profile_preserves_production_pipeline", "proved", legality.obligation(),
          Map.of(), null, null, null,
          expansions, profile.scheduler().maxFunctionExpansions()));
      return true;
    }
    CostEstimate baseline = costModel.estimate(function);
    CostEstimate transformed = costModel.estimate(candidate);
    AllocationEstimate allocation = allocator.estimate(candidate, target);
    boolean profitable = transformed.robustScore(profile.scheduler())
        < baseline.robustScore(profile.scheduler());
    if (profitable) function.replaceWith(candidate);
    trace.accept(new DecisionTraceSink.Decision(profile.id(), profile.evidenceLevel().name().toLowerCase(),
        candidateId, "machine-function",
        function.getName(), profitable ? "applied" : "rejected",
        profitable ? "robust_cost_improved" : "robust_cost_not_improved",
        "proved", legality.obligation(), Map.of(), baseline, transformed, allocation,
        expansions, profile.scheduler().maxFunctionExpansions()));
    return profitable;
  }
}
