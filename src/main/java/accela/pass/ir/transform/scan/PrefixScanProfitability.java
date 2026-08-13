package accela.pass.ir.transform.scan;

import accela.ir.Constant;

/** Cost gate for replacing a quadratic repeated scan with one linear incremental scan. */
final class PrefixScanProfitability {
  private PrefixScanProfitability() {}

  static boolean isProfitable(PrefixScanCandidate candidate) {
    if (candidate.outerBound() instanceof Constant.Int bound) {
      // Tiny constant domains do not amortize the extra loop-carried state and CFG cleanup.
      return bound.value >= 4;
    }

    // A dynamic nonempty domain removes a nested loop whose body contains a load and an i32 add.
    // The replacement executes the same pure term exactly once per output lane, reducing O(N^2)
    // work to O(N), while adding only one loop-carried PHI (and two i32 subs for suffix traversal).
    return candidate.termInstructions().stream()
        .anyMatch(instruction -> instruction.getOpcode() == accela.ir.Instruction.Opcode.LOAD);
  }
}
