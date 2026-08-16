package accela.cost;

/** Decomposed estimate used for robust relative decisions and DecisionTrace. */
public record CostEstimate(
    double cycles,
    double uncertainty,
    double criticalPath,
    double frontend,
    double resources,
    double memory,
    double branch,
    double spill,
    double codeSize) {
  public CostEstimate {
    double[] values = {cycles, uncertainty, criticalPath, frontend, resources, memory,
        branch, spill, codeSize};
    for (double value : values) {
      if (!Double.isFinite(value) || value < 0.0) {
        throw new IllegalArgumentException("cost components must be finite and non-negative");
      }
    }
  }

  public double robustScore(SchedulerPolicy policy) {
    return cycles + policy.uncertaintyWeight() * uncertainty;
  }
}
