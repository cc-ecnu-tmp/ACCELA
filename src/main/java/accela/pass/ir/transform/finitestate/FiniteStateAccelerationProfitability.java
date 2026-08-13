package accela.pass.ir.transform.finitestate;

import accela.ir.Constant;
import accela.ir.ExactI32;
import accela.pass.FiniteStateAccelerationCandidate;

/** Conservative static-storage and dynamic break-even policy for finite-state lifting. */
final class FiniteStateAccelerationProfitability {
  static final int LIFTING_LEVELS = Integer.SIZE - 1;
  static final int MAX_ACCELERATED_DOMAIN = 128;
  static final int MAX_TABLE_BYTES =
      LIFTING_LEVELS * MAX_ACCELERATED_DOMAIN * Integer.BYTES;
  static final int MAX_PASS_TABLE_BYTES = 256 * 1024;

  private static final int RUNTIME_TRIP_THRESHOLD = 8;

  record Plan(
      int runtimeTripThreshold, int minimumBound, int tableBytes, int[][] jumpTable) {
    Plan {
      if (runtimeTripThreshold < 2) {
        throw new IllegalArgumentException("runtimeTripThreshold must be at least two");
      }
      if (minimumBound < runtimeTripThreshold) {
        throw new IllegalArgumentException("minimumBound cannot precede the trip threshold");
      }
      if (tableBytes < 1 || tableBytes > MAX_TABLE_BYTES) {
        throw new IllegalArgumentException("tableBytes exceeds the finite-state resource policy");
      }
      jumpTable = copyTable(jumpTable);
    }
  }

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

    static Result rejected(String obligation) {
      return new Result(null, obligation);
    }
  }

  private FiniteStateAccelerationProfitability() {}

  static Result evaluate(
      FiniteStateAccelerationMatcher.Candidate candidate, int alreadyAllocatedTableBytes) {
    int domain = candidate.domainSize();
    long tableBytes = (long) LIFTING_LEVELS * domain * Integer.BYTES;
    if (domain > MAX_ACCELERATED_DOMAIN
        || tableBytes > MAX_TABLE_BYTES
        || alreadyAllocatedTableBytes < 0
        || (long) alreadyAllocatedTableBytes + tableBytes > MAX_PASS_TABLE_BYTES) {
      return Result.rejected(FiniteStateAccelerationCandidate.STORAGE_CODE_SIZE);
    }
    if (isIdentity(candidate.transition())) {
      return Result.rejected(FiniteStateAccelerationCandidate.PROFITABILITY);
    }

    // The jump table is static. Eight iterations already cover the one-time guard and three
    // binary-lifting steps for the smallest supported transition, while short runs retain the
    // original loop without a repeated in-loop guard.
    int threshold = RUNTIME_TRIP_THRESHOLD;
    int normalizedStart = ExactI32.normalize(((Constant.Int) candidate.inductionStart()).value);
    long minimumBound = (long) normalizedStart + threshold;
    if (minimumBound > Integer.MAX_VALUE) {
      return Result.rejected(FiniteStateAccelerationCandidate.PROFITABILITY);
    }
    if (candidate.bound() instanceof Constant.Int bound
        && candidate.inductionStart() instanceof Constant.Int start) {
      long first = ExactI32.normalize(start.value);
      long limit = ExactI32.normalize(bound.value);
      long tripCount = Math.max(0L, limit - first);
      if (tripCount < threshold) {
        return Result.rejected(FiniteStateAccelerationCandidate.PROFITABILITY);
      }
    }
    int[][] jumpTable = buildJumpTable(candidate.transition());
    return Result.accepted(
        new Plan(threshold, (int) minimumBound, (int) tableBytes, jumpTable));
  }

  private static boolean isIdentity(int[] transition) {
    for (int state = 0; state < transition.length; state++) {
      if (transition[state] != state) return false;
    }
    return true;
  }

  private static int[][] buildJumpTable(int[] transition) {
    int[][] table = new int[LIFTING_LEVELS][transition.length];
    table[0] = transition.clone();
    for (int level = 1; level < LIFTING_LEVELS; level++) {
      for (int state = 0; state < transition.length; state++) {
        table[level][state] = table[level - 1][table[level - 1][state]];
      }
    }
    return table;
  }

  private static int[][] copyTable(int[][] source) {
    int[][] copy = new int[source.length][];
    for (int index = 0; index < source.length; index++) copy[index] = source[index].clone();
    return copy;
  }
}
