package accela.pass;

import java.util.EnumSet;
import java.util.Set;

/** Immutable analysis-validity state carried by an R2 Beam node. */
public final class R2AnalysisState {
  private final Set<R2PassOccurrence.Analysis> valid;

  private R2AnalysisState(Set<R2PassOccurrence.Analysis> valid) {
    this.valid = Set.copyOf(valid);
  }

  public static R2AnalysisState empty() { return new R2AnalysisState(Set.of()); }

  public Set<R2PassOccurrence.Analysis> valid() { return valid; }

  public R2AnalysisState recompute(Set<R2PassOccurrence.Analysis> recomputed) {
    if (recomputed == null || recomputed.stream().anyMatch(java.util.Objects::isNull)) {
      throw new IllegalArgumentException("R2 recomputed analysis set is invalid");
    }
    EnumSet<R2PassOccurrence.Analysis> next = valid.isEmpty()
        ? EnumSet.noneOf(R2PassOccurrence.Analysis.class) : EnumSet.copyOf(valid);
    next.addAll(recomputed);
    return new R2AnalysisState(next);
  }

  public R2AnalysisState apply(
      R2PassOccurrence occurrence, Set<R2PassOccurrence.Analysis> recomputed) {
    if (occurrence == null) throw new IllegalArgumentException("R2 occurrence is required");
    if (recomputed == null || recomputed.stream().anyMatch(java.util.Objects::isNull)) {
      throw new IllegalArgumentException("R2 recomputed analysis set is invalid");
    }
    EnumSet<R2PassOccurrence.Analysis> before = valid.isEmpty()
        ? EnumSet.noneOf(R2PassOccurrence.Analysis.class) : EnumSet.copyOf(valid);
    before.addAll(recomputed);
    if (!before.containsAll(occurrence.requiredAnalyses())) {
      EnumSet<R2PassOccurrence.Analysis> missing =
          EnumSet.copyOf(occurrence.requiredAnalyses());
      missing.removeAll(before);
      throw new IllegalStateException(
          "R2 pass " + occurrence.id() + " is missing analyses " + missing);
    }
    before.removeAll(occurrence.invalidatedAnalyses());
    return new R2AnalysisState(before);
  }
}
