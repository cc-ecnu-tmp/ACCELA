package accela.cost;

import accela.backend.machine.AllocatedMachineVerifier;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineVerifier;
import accela.backend.regalloc.AllocationEstimate;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVTarget;
import accela.pass.R2PassOccurrence;
import accela.pass.R2PassRegistry;
import accela.pass.R2AnalysisState;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.BiPredicate;
import java.util.function.Predicate;

/** Unified per-function R2 Beam for MIR cleanup, DryRunRA, RA and post-RA layout. */
public final class R2MachineBeamScheduler {
  @FunctionalInterface
  public interface Transform extends Predicate<MachineFunction> {}

  public record Step(R2PassOccurrence occurrence, Transform transform) {
    public Step {
      if (occurrence == null || transform == null) {
        throw new IllegalArgumentException("R2 machine step is incomplete");
      }
    }
  }

  public record Plan(MachineFunction preRaFunction, boolean branchFolding,
      boolean postRaPlacement, List<String> decisions) {
    public Plan {
      decisions = List.copyOf(decisions);
    }
  }

  private final TargetProfile profile;
  private final SchedulerPolicy policy;
  private final DecisionTraceSink trace;
  private final RegisterAllocator allocator;
  private final RISCVTarget target;
  private final MachineCostModel costModel;
  private int expansions;

  public R2MachineBeamScheduler(TargetProfile profile, DecisionTraceSink trace,
      RegisterAllocator allocator, RISCVTarget target) {
    this(profile, trace, allocator, target, profile.scheduler());
  }

  R2MachineBeamScheduler(TargetProfile profile, DecisionTraceSink trace,
      RegisterAllocator allocator, RISCVTarget target, SchedulerPolicy policy) {
    this.profile = java.util.Objects.requireNonNull(profile, "profile");
    this.trace = java.util.Objects.requireNonNull(trace, "trace");
    this.allocator = java.util.Objects.requireNonNull(allocator, "allocator");
    this.target = java.util.Objects.requireNonNull(target, "target");
    this.policy = java.util.Objects.requireNonNull(policy, "policy");
    costModel = new MachineCostModel(profile, allocator, target);
  }

  public Plan schedule(MachineFunction lowered, List<Step> preRaSteps,
      BiPredicate<MachineFunction, AllocationResult> branchFolding,
      Predicate<MachineFunction> postRaPlacement) {
    expansions = 0;
    MachineFunction fullPreRa = lowered.deepCopy();
    for (Step step : preRaSteps) {
      step.transform().test(fullPreRa);
      MachineVerifier.verify(fullPreRa);
    }
    List<String> fullDecisions = preRaSteps.stream()
        .map(step -> step.occurrence().id() + "=APPLY").toList();
    Allocated full = allocate(fullPreRa, fullDecisions, true, true,
        branchFolding, postRaPlacement);
    if (!profile.calibrated() || !policy.enabled()) return full.plan();

    List<State> beam = List.of(state(lowered.deepCopy(), List.of(), Set.of(),
        R2AnalysisState.empty()));
    Set<String> scheduledIds = preRaSteps.stream()
        .map(step -> step.occurrence().id())
        .collect(java.util.stream.Collectors.toUnmodifiableSet());
    for (int depth = 0; depth < preRaSteps.size(); depth++) {
      List<State> expanded = new ArrayList<>();
      for (State state : beam) {
        List<Step> available = preRaSteps.stream()
            .filter(step -> !state.decided().contains(step.occurrence().id()))
            .filter(step -> step.occurrence().dependencies().stream()
                .filter(scheduledIds::contains).allMatch(state.decided()::contains))
            .toList();
        if (available.isEmpty()) {
          throw new IllegalStateException("R2 MIR Beam/DAG has no legal occurrence at depth "
              + depth + " for " + lowered.getName());
        }
        for (Step step : available) {
          if (!step.occurrence().required()) {
            if (!reserve(step.occurrence(), lowered.getName())) {
              return budgetResult(full, lowered.getName(), beam, preRaSteps,
                  expanded, branchFolding, postRaPlacement);
            }
            expanded.add(new State(state.function(), state.cost(), state.allocation(),
                append(state.decisions(), step.occurrence().id() + "=SKIP"),
                add(state.decided(), step.occurrence().id()), state.analyses()));
            emit(step.occurrence(), lowered.getName(), "rejected", "beam_skip_branch",
                state.cost(), state.cost(), state.allocation());
          }
          if (!reserve(step.occurrence(), lowered.getName())) {
            return budgetResult(full, lowered.getName(), beam, preRaSteps,
                expanded, branchFolding, postRaPlacement);
          }
          R2AnalysisState afterPass = state.analyses().apply(
              step.occurrence(), step.occurrence().requiredAnalyses());
          MachineFunction candidate = state.function().deepCopy();
          boolean changed = step.transform().test(candidate);
          MachineVerifier.verify(candidate);
          State applied = changed ? state(candidate,
              append(state.decisions(), step.occurrence().id() + "=APPLY"),
              add(state.decided(), step.occurrence().id()), afterPass)
              : unchangedAfterBoundary(state, step.occurrence());
          expanded.add(applied);
          emit(step.occurrence(), lowered.getName(), changed ? "considered" : "rejected",
              changed ? "proved_and_costed" : "no_change", state.cost(),
              changed ? applied.cost() : null, changed ? applied.allocation() : null);
        }
      }
      expanded.sort(stateComparator());
      beam = List.copyOf(expanded.subList(0,
          Math.min(policy.beamWidth(), expanded.size())));
    }

    List<Allocated> candidates = new ArrayList<>();
    for (State state : beam) {
      candidates.add(allocate(state.function(), state.decisions(), true, true,
          branchFolding, postRaPlacement));
      candidates.add(allocate(state.function(), state.decisions(), true, false,
          branchFolding, postRaPlacement));
      candidates.add(allocate(state.function(), state.decisions(), false, true,
          branchFolding, postRaPlacement));
      candidates.add(allocate(state.function(), state.decisions(), false, false,
          branchFolding, postRaPlacement));
    }
    Allocated selected = candidates.stream()
        .filter(candidate -> safelyDominates(full, candidate))
        .min(Comparator.comparingDouble(candidate ->
            candidate.cost().robustScore(policy)))
        .orElse(full);
    emitFinal(lowered.getName(), full, selected,
        "best_validated_r2_mir_state", "full_retained");
    return selected.plan();
  }

  private Allocated allocate(MachineFunction source, List<String> decisions,
      boolean foldBranches, boolean placeBlocks,
      BiPredicate<MachineFunction, AllocationResult> branchFolding,
      Predicate<MachineFunction> postRaPlacement) {
    MachineFunction function = source.deepCopy();
    AllocationEstimate dryRun = allocator.estimate(function, target);
    AllocationResult allocation = allocator.allocate(function, target);
    if (foldBranches) branchFolding.test(function, allocation);
    if (placeBlocks) postRaPlacement.test(function);
    AllocatedMachineVerifier.verify(function, allocation);
    List<String> completeDecisions = append(decisions,
        R2PassRegistry.production().family(R2PassRegistry.BRANCH_FOLDING).getFirst().id()
            + (foldBranches ? "=APPLY" : "=SKIP"));
    completeDecisions = append(completeDecisions,
        R2PassRegistry.production().family(R2PassRegistry.BLOCK_PLACEMENT).getLast().id()
            + (placeBlocks ? "=APPLY" : "=SKIP"));
    return new Allocated(new Plan(source.deepCopy(), foldBranches, placeBlocks, completeDecisions),
        costModel.estimate(function, dryRun), costModel.unboundedLoopInstructionSlopes(function),
        costModel.machineCycleUpperBounds(function), costModel.machineCycleLowerBounds(function),
        dryRun, foldBranches, placeBlocks);
  }

  private boolean safelyDominates(Allocated baseline, Allocated candidate) {
    if (!slopesNoWorse(baseline.slopes(), candidate.slopes())) return false;
    if (!cycleEnvelopesNoWorse(baseline.cycleLowerBounds(),
        candidate.cycleUpperBounds())) return false;
    CostEstimate base = baseline.cost();
    CostEstimate next = candidate.cost();
    if (!(next.robustScore(policy) < base.robustScore(policy))) return false;
    return noWorse(next.criticalPath(), base.criticalPath())
        && noWorse(next.frontend(), base.frontend())
        && noWorse(next.resources(), base.resources())
        && noWorse(next.memory(), base.memory())
        && noWorse(next.branch(), base.branch())
        && noWorse(next.spill(), base.spill())
        && noWorse(next.codeSize(), base.codeSize());
  }

  private static boolean slopesNoWorse(Map<String, Double> baseline, Map<String, Double> candidate) {
    List<Double> base = baseline.values().stream().sorted(Comparator.reverseOrder()).toList();
    List<Double> next = candidate.values().stream().sorted(Comparator.reverseOrder()).toList();
    for (int index = 0; index < Math.max(base.size(), next.size()); index++) {
      double left = index < base.size() ? base.get(index) : 0.0;
      double right = index < next.size() ? next.get(index) : 0.0;
      if (!noWorse(right, left)) return false;
    }
    return true;
  }

  private static boolean cycleEnvelopesNoWorse(
      List<Double> baselineLowerBounds, List<Double> candidateUpperBounds) {
    if (candidateUpperBounds.size() > baselineLowerBounds.size()) return false;
    for (int index = 0; index < baselineLowerBounds.size(); index++) {
      double candidate = index < candidateUpperBounds.size()
          ? candidateUpperBounds.get(index) : 0.0;
      if (!noWorse(candidate, baselineLowerBounds.get(index))) return false;
    }
    return true;
  }

  private static boolean noWorse(double candidate, double baseline) {
    return candidate <= baseline
        + Math.ulp(Math.max(Math.abs(candidate), Math.abs(baseline))) * 4.0;
  }

  private State state(MachineFunction function, List<String> decisions, Set<String> decided,
      R2AnalysisState analyses) {
    if (containsPhi(function)) {
      return new State(function, null, null, decisions, decided, analyses);
    }
    R2AnalysisState withDryRun = analyses.recompute(Set.of(
        R2PassOccurrence.Analysis.LIVENESS, R2PassOccurrence.Analysis.INTERFERENCE));
    return new State(function, costModel.estimate(function), allocator.estimate(function, target),
        decisions, decided, withDryRun);
  }

  private State unchangedAfterBoundary(State state, R2PassOccurrence occurrence) {
    List<String> decisions = append(state.decisions(), occurrence.id() + "=APPLY");
    Set<String> decided = add(state.decided(), occurrence.id());
    if (state.cost() == null && !containsPhi(state.function())) {
      return state(state.function(), decisions, decided, state.analyses());
    }
    return new State(state.function(), state.cost(), state.allocation(), decisions, decided,
        state.analyses());
  }

  private Comparator<State> stateComparator() {
    return Comparator.<State>comparingDouble(state -> state.cost() == null
            ? Double.POSITIVE_INFINITY : state.cost().robustScore(policy))
        .thenComparing(state -> String.join("\u0000", state.decisions()));
  }

  private static boolean containsPhi(MachineFunction function) {
    return function.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode()
            == accela.backend.machine.MachineOpcode.PHI);
  }

  private boolean reserve(R2PassOccurrence occurrence, String function) {
    if (expansions >= policy.maxFunctionExpansions()) {
      trace.accept(new DecisionTraceSink.Decision(profile.id(),
          profile.evidenceLevel().name().toLowerCase(), occurrence.id(), "machine-function",
          function, "rejected", "budget_exhausted", "not_evaluated",
          occurrence.legalityObligation(), Map.of("scope", "function"), null, null, null,
          expansions, policy.maxFunctionExpansions()));
      return false;
    }
    expansions++;
    return true;
  }

  private Plan budgetResult(Allocated full, String function, List<State> beam,
      List<Step> steps, List<State> expanded,
      BiPredicate<MachineFunction, AllocationResult> branchFolding,
      Predicate<MachineFunction> postRaPlacement) {
    Set<String> scheduledIds = steps.stream().map(step -> step.occurrence().id())
        .collect(java.util.stream.Collectors.toUnmodifiableSet());
    List<Allocated> completed = new ArrayList<>();
    List<State> frontier = new ArrayList<>(beam);
    frontier.addAll(expanded);
    for (State initial : frontier) {
      State state = initial;
      while (state.decided().size() < steps.size()) {
        State current = state;
        Step step = steps.stream()
            .filter(candidate -> !current.decided().contains(candidate.occurrence().id()))
            .filter(candidate -> candidate.occurrence().dependencies().stream()
                .filter(scheduledIds::contains).allMatch(current.decided()::contains))
            .findFirst().orElseThrow(() -> new IllegalStateException(
                "R2 MIR budget completion cannot advance " + function));
        R2AnalysisState afterPass = state.analyses().apply(
            step.occurrence(), step.occurrence().requiredAnalyses());
        MachineFunction candidate = state.function().deepCopy();
        step.transform().test(candidate);
        MachineVerifier.verify(candidate);
        state = state(candidate,
            append(state.decisions(), step.occurrence().id() + "=APPLY_AFTER_BUDGET"),
            add(state.decided(), step.occurrence().id()), afterPass);
      }
      completed.add(allocate(state.function(), state.decisions(), true, true,
          branchFolding, postRaPlacement));
      completed.add(allocate(state.function(), state.decisions(), true, false,
          branchFolding, postRaPlacement));
      completed.add(allocate(state.function(), state.decisions(), false, true,
          branchFolding, postRaPlacement));
      completed.add(allocate(state.function(), state.decisions(), false, false,
          branchFolding, postRaPlacement));
    }
    Allocated selected = completed.stream().filter(candidate -> safelyDominates(full, candidate))
        .min(Comparator.comparingDouble(candidate ->
            candidate.cost().robustScore(policy)))
        .orElse(full);
    emitFinal(function, full, selected, "budget_exhausted_best_validated",
        "budget_exhausted_full_retained");
    return selected.plan();
  }

  private void emit(R2PassOccurrence occurrence, String function, String status, String reason,
      CostEstimate baseline, CostEstimate transformed, AllocationEstimate allocation) {
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), occurrence.id(), "machine-function",
        function, status, reason, "no_change".equals(reason) ? "not_applicable" : "proved",
        occurrence.legalityObligation(), Map.of(), baseline, transformed, allocation,
        expansions, policy.maxFunctionExpansions()));
  }

  private void emitFinal(String function, Allocated full, Allocated selected,
      String appliedReason, String retainedReason) {
    LinkedHashMap<String, String> parameters = new LinkedHashMap<>();
    parameters.put("branch_folding", Boolean.toString(selected.branchFolding()));
    parameters.put("post_ra_placement", Boolean.toString(selected.postRaPlacement()));
    parameters.put("sequence", String.join(",", selected.plan().decisions()));
    parameters.put("baseline_loop_slopes", sortedSlopes(full.slopes()));
    parameters.put("selected_loop_slopes", sortedSlopes(selected.slopes()));
    parameters.put("baseline_min_cycle_costs", join(full.cycleLowerBounds()));
    parameters.put("selected_max_cycle_costs", join(selected.cycleUpperBounds()));
    boolean applied = selected != full;
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), "r2.mir.beam.final", "machine-function",
        function, applied ? "applied" : "rejected",
        applied ? appliedReason : retainedReason,
        applied ? "proved" : "not_applicable", "r2.mir.beam.validated-state", parameters,
        full.cost(), selected.cost(), selected.dryRun(), expansions,
        policy.maxFunctionExpansions()));
  }

  private static List<String> append(List<String> values, String value) {
    List<String> result = new ArrayList<>(values);
    result.add(value);
    return List.copyOf(result);
  }

  private static Set<String> add(Set<String> values, String value) {
    java.util.LinkedHashSet<String> result = new java.util.LinkedHashSet<>(values);
    result.add(value);
    return Set.copyOf(result);
  }

  private static String sortedSlopes(Map<String, Double> slopes) {
    return slopes.values().stream().sorted(Comparator.reverseOrder())
        .map(value -> Double.toString(value))
        .collect(java.util.stream.Collectors.joining(","));
  }

  private static String join(List<Double> values) {
    return values.stream().map(value -> Double.toString(value))
        .collect(java.util.stream.Collectors.joining(","));
  }

  private record State(MachineFunction function, CostEstimate cost,
      AllocationEstimate allocation, List<String> decisions, Set<String> decided,
      R2AnalysisState analyses) {}

  private record Allocated(Plan plan, CostEstimate cost, Map<String, Double> slopes,
      List<Double> cycleUpperBounds, List<Double> cycleLowerBounds,
      AllocationEstimate dryRun, boolean branchFolding, boolean postRaPlacement) {}
}
