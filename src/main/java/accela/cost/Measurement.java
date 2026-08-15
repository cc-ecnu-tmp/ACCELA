package accela.cost;

/** One validated TargetLab estimate with retained dispersion metadata. */
public record Measurement(double median, double mad, int sampleCount, String source) {
  public Measurement {
    if (!Double.isFinite(median) || median <= 0.0) {
      throw new IllegalArgumentException("measurement median must be finite and positive");
    }
    if (!Double.isFinite(mad) || mad < 0.0) {
      throw new IllegalArgumentException("measurement MAD must be finite and non-negative");
    }
    if (sampleCount < 1) throw new IllegalArgumentException("measurement needs samples");
    if (source == null || source.isBlank()) {
      throw new IllegalArgumentException("measurement source is required");
    }
  }

  public double relativeMad() {
    return mad / median;
  }
}
