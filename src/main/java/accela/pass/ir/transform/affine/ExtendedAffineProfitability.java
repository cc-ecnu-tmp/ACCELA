package accela.pass.ir.transform.affine;

import java.util.Objects;

/** Deterministic cost model for guarded extended-affine loop summarization. */
public final class ExtendedAffineProfitability {
  private static final int MAXIMUM_SUMMARY_ARITHMETIC = 96;
  private static final int MAXIMUM_RUNTIME_TRIP_THRESHOLD = 64;

  public record Assessment(
      boolean profitable,
      int minimumTripCount,
      int estimatedSummaryArithmetic,
      int bodyArithmeticPerIteration) {}

  private ExtendedAffineProfitability() {}

  public static Assessment assess(ExtendedAffineMatcher.Plan plan) {
    Objects.requireNonNull(plan, "plan");
    int degree = plan.maximumDeltaDegree();
    int sharedArithmetic = 4; // remaining count, current iteration, and final induction
    if (plan.inductionStep() > 1) sharedArithmetic += 3;
    if (degree >= 1) sharedArithmetic += 7; // exact C(n, 2)
    if (degree >= 2) sharedArithmetic += 34; // exact C(n, 3), then sum of squares

    int recurrenceArithmetic = 0;
    for (ExtendedAffineMatcher.StateRecurrence recurrence : plan.recurrences()) {
      int recurrenceDegree = recurrence.delta().degree();
      recurrenceArithmetic += switch (recurrenceDegree) {
        case 0 -> 2;
        case 1 -> 7;
        case 2 -> 13;
        default -> throw new IllegalStateException("unsupported polynomial degree");
      };
    }
    int summaryArithmetic = sharedArithmetic + recurrenceArithmetic;
    int bodyArithmetic = plan.bodyArithmeticInstructions();
    int minimumTripCount = bodyArithmetic == 0
        ? Integer.MAX_VALUE
        : Math.max(4, Math.floorDiv(summaryArithmetic + bodyArithmetic - 1, bodyArithmetic) + 2);
    boolean profitable = bodyArithmetic >= 2
        && summaryArithmetic <= MAXIMUM_SUMMARY_ARITHMETIC
        && minimumTripCount <= MAXIMUM_RUNTIME_TRIP_THRESHOLD;
    return new Assessment(
        profitable, minimumTripCount, summaryArithmetic, bodyArithmetic);
  }
}
