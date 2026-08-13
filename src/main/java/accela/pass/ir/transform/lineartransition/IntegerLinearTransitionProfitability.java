package accela.pass.ir.transform.lineartransition;

import accela.ir.Constant;
import accela.pass.IntegerLinearTransitionCandidate;

/** Conservative static and runtime cost policy for matrix binary lifting. */
final class IntegerLinearTransitionProfitability {
  record Result(Plan plan, String rejectedObligationId) {
    Result {
      if ((plan == null) == (rejectedObligationId == null)) {
        throw new IllegalArgumentException(
            "exactly one of plan and rejectedObligationId is required");
      }
    }

    static Result accepted(Plan plan) {
      return new Result(plan, null);
    }

    static Result rejected() {
      return new Result(null, IntegerLinearTransitionCandidate.PROFITABILITY);
    }
  }

  /** Minimum remaining iteration count needed before entering the generated lifting loop. */
  record Plan(int runtimeTripThreshold) {
    Plan {
      if (runtimeTripThreshold < 2) {
        throw new IllegalArgumentException("runtimeTripThreshold must be at least two");
      }
    }
  }

  private IntegerLinearTransitionProfitability() {}

  static Result evaluate(IntegerLinearTransitionMatcher.Candidate candidate) {
    if (isIdentity(candidate.homogeneousTransition())) return Result.rejected();

    int threshold = switch (candidate.stateDimension()) {
      case 1 -> 8;
      case 2 -> 32;
      case 3 -> 96;
      default -> throw new IllegalArgumentException(
          "unsupported state dimension " + candidate.stateDimension());
    };
    if (candidate.bound() instanceof Constant.Int bound
        && candidate.inductionStart() instanceof Constant.Int start) {
      long exactTripCount = (long) (int) bound.value - (int) start.value;
      if (exactTripCount < threshold) return Result.rejected();
    }
    return Result.accepted(new Plan(threshold));
  }

  private static boolean isIdentity(int[][] matrix) {
    for (int row = 0; row < matrix.length; row++) {
      for (int column = 0; column < matrix.length; column++) {
        int expected = row == column ? 1 : 0;
        if (matrix[row][column] != expected) return false;
      }
    }
    return true;
  }
}
