package accela.pass;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Immutable legal R2 decision prefix suitable for storage in a Beam state. */
public final class R2ScheduleState {
  public enum Action { APPLY, SKIP }

  public record Decision(
      String occurrenceId,
      Action action,
      Set<R2PassOccurrence.Analysis> recomputedAnalyses) {
    public Decision {
      if (occurrenceId == null || occurrenceId.isBlank()) {
        throw new IllegalArgumentException("R2 decision occurrence id is required");
      }
      if (action == null) throw new IllegalArgumentException("R2 decision action is required");
      if (recomputedAnalyses == null
          || recomputedAnalyses.stream().anyMatch(java.util.Objects::isNull)) {
        throw new IllegalArgumentException("R2 decision analysis set is invalid");
      }
      recomputedAnalyses = Set.copyOf(recomputedAnalyses);
      if (action == Action.SKIP && !recomputedAnalyses.isEmpty()) {
        throw new IllegalArgumentException("skipped R2 pass cannot recompute analyses");
      }
    }
  }

  private final R2PassRegistry registry;
  private final List<Decision> decisions;
  private final Set<String> decided;
  private final R2AnalysisState analyses;

  private R2ScheduleState(R2PassRegistry registry, List<Decision> decisions,
      Set<String> decided, R2AnalysisState analyses) {
    this.registry = registry;
    this.decisions = List.copyOf(decisions);
    this.decided = Set.copyOf(decided);
    this.analyses = analyses;
  }

  public static R2ScheduleState initial(R2PassRegistry registry) {
    if (registry == null) throw new IllegalArgumentException("R2 registry is required");
    return new R2ScheduleState(registry, List.of(), Set.of(), R2AnalysisState.empty());
  }

  public List<R2PassOccurrence> available() {
    return registry.all().stream()
        .filter(occurrence -> !decided.contains(occurrence.id()))
        .filter(occurrence -> decided.containsAll(occurrence.dependencies()))
        .toList();
  }

  public R2ScheduleState decide(Decision decision) {
    R2PassOccurrence occurrence = registry.require(decision.occurrenceId());
    if (decided.contains(occurrence.id())) {
      throw new IllegalArgumentException("R2 occurrence already decided: " + occurrence.id());
    }
    if (!decided.containsAll(occurrence.dependencies())) {
      throw new IllegalArgumentException(
          "R2 occurrence dependencies are not decided: " + occurrence.id());
    }
    if (occurrence.required() && decision.action() == Action.SKIP) {
      throw new IllegalArgumentException("required R2 occurrence cannot be skipped: " + occurrence.id());
    }
    R2AnalysisState nextAnalyses = decision.action() == Action.APPLY
        ? analyses.apply(occurrence, decision.recomputedAnalyses()) : analyses;
    List<Decision> nextDecisions = new ArrayList<>(decisions);
    nextDecisions.add(decision);
    Set<String> nextDecided = new LinkedHashSet<>(decided);
    nextDecided.add(occurrence.id());
    return new R2ScheduleState(registry, nextDecisions, nextDecided, nextAnalyses);
  }

  public boolean complete() { return decisions.size() == registry.all().size(); }

  public List<Decision> decisions() { return decisions; }

  public R2AnalysisState analyses() { return analyses; }

  public R2PipelineProfile toProfile() {
    if (!complete()) throw new IllegalStateException("R2 decision prefix is incomplete");
    return new R2PipelineProfile(registry, decisions);
  }
}
