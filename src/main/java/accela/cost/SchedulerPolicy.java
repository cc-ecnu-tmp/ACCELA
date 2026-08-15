package accela.cost;

/** Deterministic search limits embedded with the target profile. */
public record SchedulerPolicy(
    int beamWidth,
    int maxFunctionExpansions,
    int maxModuleExpansions,
    double uncertaintyWeight,
    boolean enabled) {
  public SchedulerPolicy {
    if (beamWidth < 1 || beamWidth > 64) throw new IllegalArgumentException("beamWidth must be 1..64");
    if (maxFunctionExpansions < beamWidth) {
      throw new IllegalArgumentException("function expansion budget is smaller than beam width");
    }
    if (maxModuleExpansions < maxFunctionExpansions) {
      throw new IllegalArgumentException("module expansion budget is smaller than function budget");
    }
    if (!Double.isFinite(uncertaintyWeight) || uncertaintyWeight < 0.0) {
      throw new IllegalArgumentException("uncertaintyWeight must be finite and non-negative");
    }
  }
}
