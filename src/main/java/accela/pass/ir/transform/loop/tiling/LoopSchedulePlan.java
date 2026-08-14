package accela.pass.ir.transform.loop.tiling;

/** Deterministic schedule choice used by the RV64GC candidate. */
public record LoopSchedulePlan(int tile, long estimatedCost, boolean profitable) {
  public LoopSchedulePlan {
    if (tile != 4 && tile != 8 && tile != 16 && tile != 32) {
      throw new IllegalArgumentException("unsupported tile size: " + tile);
    }
    if (estimatedCost < 0) throw new IllegalArgumentException("estimated cost must be non-negative");
  }
}
