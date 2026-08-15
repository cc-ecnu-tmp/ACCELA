package accela.backend.regalloc;

/** Non-mutating result from the real simplify/coalesce/spill-selection allocator path. */
public record AllocationEstimate(
    int predictedSpills,
    double spillWeight,
    int maxIntegerLive,
    int maxFloatLive,
    int coalescingLoss) {
  public AllocationEstimate {
    if (predictedSpills < 0 || spillWeight < 0.0 || maxIntegerLive < 0
        || maxFloatLive < 0 || coalescingLoss < 0 || !Double.isFinite(spillWeight)) {
      throw new IllegalArgumentException("invalid allocation estimate");
    }
  }
}
