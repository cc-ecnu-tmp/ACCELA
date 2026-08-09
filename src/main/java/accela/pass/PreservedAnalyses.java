package accela.pass;

import java.util.LinkedHashSet;
import java.util.Set;

/**
 * Describes which analysis results remain valid after a transform pass runs.
 *
 * <p>- preserve everything
 *
 * <p>- preserve nothing
 *
 * <p>- preserve a named subset of analysis result types
 *
 * <p>Transform passes use {@link #all()} only when they made no change and a non-all result when
 * they changed IR. Pass managers rely on this contract to report the pass's explicit modification
 * result; structural metric deltas are diagnostic data and are not used to infer modification.
 */
public final class PreservedAnalyses {
  private final boolean preserveAll;
  private final Set<Class<?>> preserved = new LinkedHashSet<>();

  private PreservedAnalyses(boolean preserveAll) {
    this.preserveAll = preserveAll;
  }

  /** Returns a state that preserves every registered analysis result. */
  public static PreservedAnalyses all() {
    return new PreservedAnalyses(true);
  }

  /** Returns a state that preserves no analysis results. */
  public static PreservedAnalyses none() {
    return new PreservedAnalyses(false);
  }

  /** Marks one analysis result type as preserved. */
  public PreservedAnalyses preserve(Class<?> analysisType) {
    if (!preserveAll) preserved.add(analysisType);
    return this;
  }

  /** Returns whether all analyses are preserved. */
  public boolean preservesAll() {
    return preserveAll;
  }

  /** Returns whether no analysis is preserved. */
  public boolean preservesNone() {
    return !preserveAll && preserved.isEmpty();
  }

  /** Returns the transformation's explicit modified result under the pass-result contract. */
  public boolean isModified() {
    return !preserveAll;
  }

  /** Returns whether the given analysis result type remains valid. */
  public boolean isPreserved(Class<?> analysisType) {
    return preserveAll || preserved.contains(analysisType);
  }

  /** Intersects this preserved set with another one. */
  public PreservedAnalyses intersect(PreservedAnalyses other) {
    if (preserveAll) {
      if (other.preserveAll) return all();
      PreservedAnalyses result = none();
      for (Class<?> analysisType : other.preserved) {
        result.preserve(analysisType);
      }
      return result;
    }
    if (other.preserveAll) {
      PreservedAnalyses result = none();
      for (Class<?> analysisType : preserved) {
        result.preserve(analysisType);
      }
      return result;
    }
    PreservedAnalyses result = none();
    for (Class<?> analysisType : preserved) {
      if (other.preserved.contains(analysisType)) result.preserve(analysisType);
    }
    return result;
  }
}
