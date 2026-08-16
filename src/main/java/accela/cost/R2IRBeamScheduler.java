package accela.cost;

import accela.ir.IRSnapshot;
import accela.pass.R2IRPassExecutor;
import accela.pass.R2PassOccurrence;
import accela.pass.R2PassRegistry;
import accela.pass.R2ScheduleState;
import accela.pass.ir.verify.IRVerifier;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Bounded transactional R2 Beam over every registered production IR occurrence. */
public final class R2IRBeamScheduler {
  private final TargetProfile profile;
  private final SchedulerPolicy policy;
  private final DecisionTraceSink trace;
  private final ModuleCostEvaluator evaluator;
  private final R2IRPassExecutor executor = new R2IRPassExecutor();
  private int expansions;

  public R2IRBeamScheduler(TargetProfile profile, DecisionTraceSink trace) {
    this(profile, trace, profile.scheduler());
  }

  R2IRBeamScheduler(TargetProfile profile, DecisionTraceSink trace, SchedulerPolicy policy) {
    this.profile = java.util.Objects.requireNonNull(profile, "profile");
    this.trace = java.util.Objects.requireNonNull(trace, "trace");
    this.policy = java.util.Objects.requireNonNull(policy, "policy");
    evaluator = new ModuleCostEvaluator(profile);
  }

  public accela.ir.Module schedule(
      accela.ir.Module productionFull, accela.ir.Module source) {
    expansions = 0;
    IRVerifier.verifyModule(productionFull);
    IRVerifier.verifyModule(source);
    ModuleCostEvaluator.Evaluation full = evaluator.evaluate(productionFull);
    if (!profile.calibrated() || !policy.enabled()) {
      emitFinal("rejected", !profile.calibrated()
          ? "profile_not_calibrated" : "scheduler_disabled", full, full, List.of(), 0);
      return IRSnapshot.deepCopy(productionFull);
    }

    State seed = new State(IRSnapshot.deepCopy(source),
        R2ScheduleState.initial(R2PassRegistry.production()), evaluator.evaluate(source));
    List<State> beam = List.of(seed);
    int irOccurrences = (int) R2PassRegistry.production().all().stream()
        .filter(occurrence -> occurrence.stage() == accela.pass.PassDescriptor.Stage.IR).count();
    for (int depth = 0; depth < irOccurrences; depth++) {
      List<State> expanded = new ArrayList<>();
      for (State state : beam) {
        List<R2PassOccurrence> available = state.schedule().available().stream()
            .filter(occurrence -> occurrence.stage() == accela.pass.PassDescriptor.Stage.IR)
            .toList();
        if (available.isEmpty()) {
          throw new IllegalStateException("R2 IR Beam/DAG has no legal occurrence at depth "
              + depth);
        }
        for (R2PassOccurrence occurrence : available) {
          if (!occurrence.required()) {
            if (!reserve(occurrence)) {
              return budgetResult(productionFull, full, beam, expanded);
            }
            expanded.add(skip(state, occurrence));
          }
          if (!reserve(occurrence)) {
            return budgetResult(productionFull, full, beam, expanded);
          }
          expanded.add(apply(state, occurrence));
        }
      }
      expanded.sort(stateComparator());
      beam = List.copyOf(expanded.subList(0,
          Math.min(policy.beamWidth(), expanded.size())));
    }

    State selected = beam.stream()
        .filter(state -> evaluator.safelyDominates(full, state.evaluation()))
        .min(stateComparator())
        .orElse(null);
    if (selected == null) {
      emitFinal("rejected", "full_retained", full, full, List.of(), beam.size());
      return IRSnapshot.deepCopy(productionFull);
    }
    List<String> sequence = serializedDecisions(selected);
    emitFinal("applied", "best_validated_r2_ir_state", full, selected.evaluation(),
        sequence, beam.size());
    return selected.module();
  }

  private State skip(State state, R2PassOccurrence occurrence) {
    R2ScheduleState schedule = state.schedule().decide(new R2ScheduleState.Decision(
        occurrence.id(), R2ScheduleState.Action.SKIP, java.util.Set.of()));
    trace.accept(decision(occurrence, "rejected", "beam_skip_branch", state.evaluation(),
        state.evaluation(), state.evaluation().allocation()));
    return new State(state.module(), schedule, state.evaluation());
  }

  private State apply(State state, R2PassOccurrence occurrence) {
    R2ScheduleState schedule = state.schedule().decide(new R2ScheduleState.Decision(
        occurrence.id(), R2ScheduleState.Action.APPLY, occurrence.requiredAnalyses()));
    accela.ir.Module candidate = IRSnapshot.deepCopy(state.module());
    boolean changed = executor.apply(occurrence, candidate);
    ModuleCostEvaluator.Evaluation evaluation = changed
        ? evaluator.evaluate(candidate) : state.evaluation();
    trace.accept(decision(occurrence, changed ? "considered" : "rejected",
        changed ? "proved_and_costed" : "no_change", state.evaluation(),
        changed ? evaluation : null, changed ? evaluation.allocation() : null));
    return new State(changed ? candidate : state.module(), schedule, evaluation);
  }

  private Comparator<State> stateComparator() {
    return Comparator.<State>comparingDouble(
            state -> state.evaluation().cost().robustScore(policy))
        .thenComparing(state -> state.schedule().decisions().stream()
            .map(decision -> decision.occurrenceId() + "=" + decision.action().name())
            .collect(java.util.stream.Collectors.joining("\u0000")));
  }

  private DecisionTraceSink.Decision decision(R2PassOccurrence occurrence, String status,
      String reason, ModuleCostEvaluator.Evaluation baseline,
      ModuleCostEvaluator.Evaluation transformed,
      accela.backend.regalloc.AllocationEstimate allocation) {
    return new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), occurrence.id(),
        occurrence.scope() == R2PassOccurrence.Scope.FUNCTION ? "ir-function-set" : "module",
        "module", status, reason, "no_change".equals(reason) ? "not_applicable" : "proved",
        occurrence.legalityObligation(), Map.of(), baseline.cost(),
        transformed == null ? null : transformed.cost(), allocation, expansions,
        policy.maxModuleExpansions());
  }

  private void emitBudget(R2PassOccurrence occurrence) {
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), occurrence.id(), "module", "module",
        "rejected", "budget_exhausted", "not_evaluated", occurrence.legalityObligation(),
        Map.of("scope", "module"), null, null, null, expansions,
        policy.maxModuleExpansions()));
  }

  private boolean reserve(R2PassOccurrence occurrence) {
    if (expansions >= policy.maxModuleExpansions()) {
      emitBudget(occurrence);
      return false;
    }
    expansions++;
    return true;
  }

  private accela.ir.Module budgetResult(accela.ir.Module productionFull,
      ModuleCostEvaluator.Evaluation full, List<State> beam, List<State> expanded) {
    List<State> frontier = new ArrayList<>(beam);
    frontier.addAll(expanded);
    List<State> completed = frontier.stream().map(this::completeAfterBudget).toList();
    State selected = completed.stream()
        .filter(state -> evaluator.safelyDominates(full, state.evaluation()))
        .min(stateComparator()).orElse(null);
    if (selected == null) {
      emitFinal("rejected", "budget_exhausted_full_retained", full, full, List.of(),
          completed.size());
      return IRSnapshot.deepCopy(productionFull);
    }
    List<String> sequence = serializedDecisions(selected);
    emitFinal("applied", "budget_exhausted_best_validated", full, selected.evaluation(),
        sequence, completed.size());
    return selected.module();
  }

  private State completeAfterBudget(State initial) {
    State state = initial;
    while (true) {
      List<R2PassOccurrence> available = state.schedule().available().stream()
          .filter(occurrence -> occurrence.stage() == accela.pass.PassDescriptor.Stage.IR)
          .toList();
      if (available.isEmpty()) return state;
      R2PassOccurrence occurrence = available.getFirst();
      state = occurrence.required() ? apply(state, occurrence) : skip(state, occurrence);
    }
  }

  private void emitFinal(String status, String reason, ModuleCostEvaluator.Evaluation baseline,
      ModuleCostEvaluator.Evaluation transformed, List<String> sequence, int survivingStates) {
    LinkedHashMap<String, String> parameters = new LinkedHashMap<>();
    parameters.put("sequence", String.join(",", sequence));
    parameters.put("surviving_states", Integer.toString(survivingStates));
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), "r2.ir.beam.final", "module", "module",
        status, reason, "applied".equals(status) ? "proved" : "not_applicable",
        "r2.ir.beam.validated-state", parameters, baseline.cost(), transformed.cost(),
        transformed.allocation(), expansions, policy.maxModuleExpansions()));
  }

  private record State(accela.ir.Module module, R2ScheduleState schedule,
      ModuleCostEvaluator.Evaluation evaluation) {}

  private static List<String> serializedDecisions(State state) {
    return state.schedule().decisions().stream()
        .map(decision -> decision.occurrenceId() + "=" + decision.action().name())
        .toList();
  }
}
