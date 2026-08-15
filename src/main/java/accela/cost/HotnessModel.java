package accela.cost;

import java.util.ArrayList;
import java.util.List;

/** Shared robust trip-count and profile-free hotness policy. */
public final class HotnessModel {
  public static final HotnessModel DEFAULT = new HotnessModel();

  public record TripRange(long minimum, Long maximum) {
    public TripRange {
      if (minimum < 1 || maximum != null && maximum < minimum) {
        throw new IllegalArgumentException("invalid trip-count range");
      }
    }
    public static TripRange exact(long value) { return new TripRange(value, value); }
    public static TripRange finite(long minimum, long maximum) {
      return new TripRange(minimum, maximum);
    }
    public static TripRange unknown() { return new TripRange(1, null); }
  }

  public record AffineCost(double intercept, double slope) {
    public AffineCost {
      if (!Double.isFinite(intercept) || !Double.isFinite(slope)) {
        throw new IllegalArgumentException("affine costs must be finite");
      }
    }
    public double at(double trips) { return intercept + slope * trips; }
  }

  /** True only when candidate is no worse for the full range and strictly better somewhere. */
  public boolean robustlyPrefer(TripRange range, AffineCost baseline, AffineCost candidate) {
    List<Double> probes = new ArrayList<>();
    probes.add((double) range.minimum());
    if (range.maximum() != null) probes.add((double) range.maximum());
    double deltaSlope = candidate.slope() - baseline.slope();
    if (deltaSlope != 0.0) {
      double crossing = (baseline.intercept() - candidate.intercept()) / deltaSlope;
      if (crossing >= range.minimum()
          && (range.maximum() == null || crossing <= range.maximum())) probes.add(crossing);
    }
    if (range.maximum() == null && deltaSlope > 0.0) return false;
    boolean strict = false;
    for (double trips : probes) {
      double delta = candidate.at(trips) - baseline.at(trips);
      double tolerance = Math.ulp(Math.max(Math.abs(candidate.at(trips)),
          Math.abs(baseline.at(trips)))) * 4.0;
      if (delta > tolerance) return false;
      strict |= delta < -tolerance;
    }
    if (range.maximum() == null && deltaSlope < 0.0) strict = true;
    return strict;
  }

  public boolean amortizes(long knownTripCount, double perIterationBenefit, double oneTimeCost) {
    TripRange range = knownTripCount > 0 ? TripRange.exact(knownTripCount) : TripRange.unknown();
    return robustlyPrefer(range, new AffineCost(0.0, perIterationBenefit),
        new AffineCost(oneTimeCost, 0.0));
  }

  public double loopReferenceWeight(int loopDepth) {
    if (loopDepth < 0) throw new IllegalArgumentException("loop depth must be non-negative");
    double weight = Math.pow(10.0, loopDepth);
    return Double.isFinite(weight) ? weight : Double.MAX_VALUE;
  }

  public int inlineInstructionBudget(int moduleCallCount) {
    if (moduleCallCount < 1) throw new IllegalArgumentException("call count must be positive");
    return moduleCallCount == 1 ? 240 : 80;
  }

  /** With no measured edge weights, retain semantic successor preference deterministically. */
  public <T> List<T> orderUnknownSuccessors(List<T> successors) {
    return List.copyOf(successors);
  }
}
