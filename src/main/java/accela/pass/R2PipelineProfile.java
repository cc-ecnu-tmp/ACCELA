package accela.pass;

import java.util.List;
import java.util.LinkedHashSet;
import java.util.Set;

/** Complete immutable R2 pass-decision profile. */
public final class R2PipelineProfile {
  private final R2PassRegistry registry;
  private final List<R2ScheduleState.Decision> decisions;

  R2PipelineProfile(R2PassRegistry registry, List<R2ScheduleState.Decision> decisions) {
    this.registry = registry;
    this.decisions = List.copyOf(decisions);
    if (decisions.size() != registry.all().size()) {
      throw new IllegalArgumentException("R2 profile must decide every production occurrence");
    }
    Set<String> decided = new LinkedHashSet<>();
    R2AnalysisState analyses = R2AnalysisState.empty();
    int previousStage = -1;
    for (R2ScheduleState.Decision decision : this.decisions) {
      R2PassOccurrence occurrence = registry.require(decision.occurrenceId());
      if (!decided.add(occurrence.id())) {
        throw new IllegalArgumentException("duplicate R2 profile occurrence: " + occurrence.id());
      }
      if (!decided.containsAll(occurrence.dependencies())) {
        throw new IllegalArgumentException("R2 profile decision order violates dependency of "
            + occurrence.id());
      }
      if (occurrence.stage().ordinal() < previousStage) {
        throw new IllegalArgumentException("R2 profile crosses a completed phase at "
            + occurrence.id());
      }
      previousStage = occurrence.stage().ordinal();
      if (occurrence.required() && decision.action() == R2ScheduleState.Action.SKIP) {
        throw new IllegalArgumentException("required R2 occurrence cannot be skipped: "
            + occurrence.id());
      }
      if (decision.action() == R2ScheduleState.Action.APPLY) {
        analyses = analyses.apply(occurrence, decision.recomputedAnalyses());
      }
    }
    if (decided.size() != registry.all().size()) {
      throw new IllegalArgumentException("R2 profile does not cover the complete registry");
    }
  }

  public static R2PipelineProfile full() {
    R2PassRegistry registry = R2PassRegistry.production();
    R2ScheduleState state = R2ScheduleState.initial(registry);
    while (!state.complete()) {
      R2PassOccurrence occurrence = state.available().getFirst();
      state = state.decide(new R2ScheduleState.Decision(occurrence.id(),
          R2ScheduleState.Action.APPLY, occurrence.requiredAnalyses()));
    }
    return state.toProfile();
  }

  public List<R2ScheduleState.Decision> decisions() { return decisions; }

  public List<String> applied() {
    return decisions.stream()
        .filter(decision -> decision.action() == R2ScheduleState.Action.APPLY)
        .map(R2ScheduleState.Decision::occurrenceId)
        .toList();
  }

  public R2PassRegistry registry() { return registry; }
}
