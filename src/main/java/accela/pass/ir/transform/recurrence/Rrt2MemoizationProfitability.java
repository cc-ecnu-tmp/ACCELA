package accela.pass.ir.transform.recurrence;

import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.ir.analysis.recurrence.OnDemandMemoRecurrence;
import java.util.Objects;

/** Static profitability policy for the fixed-size RRT2 on-demand memo table. */
public final class Rrt2MemoizationProfitability {
  /** Covers every qualified Oracle input while limiting candidate BSS to 32 KiB per function. */
  public static final int DOMAIN_EXTENT = 64;
  public static final int TABLE_CELLS = DOMAIN_EXTENT * DOMAIN_EXTENT;
  public static final int TOTAL_STORAGE_CELLS = TABLE_CELLS * 2;

  public enum Rejection {
    NONE,
    NO_EXTERNAL_CALLER,
    INSUFFICIENT_REUSE
  }

  public record Decision(boolean profitable, Rejection rejection) {
    public Decision {
      Objects.requireNonNull(rejection, "rejection");
      if (profitable != (rejection == Rejection.NONE)) {
        throw new IllegalArgumentException("profitability and rejection disagree");
      }
    }
  }

  private Rrt2MemoizationProfitability() {}

  public static Decision evaluate(
      accela.ir.Module module, OnDemandMemoRecurrence recurrence) {
    Objects.requireNonNull(module, "module");
    Objects.requireNonNull(recurrence, "recurrence");
    if (!hasExternalCaller(module, recurrence.function())) {
      return new Decision(false, Rejection.NO_EXTERNAL_CALLER);
    }
    // A single recursive edge has no repeated subproblem to eliminate. With at least two edges,
    // identical transitions reuse the child immediately and distinct component-wise transitions
    // commute to a shared descendant.
    if (recurrence.recursiveCalls().size() < 2) {
      return new Decision(false, Rejection.INSUFFICIENT_REUSE);
    }
    return new Decision(true, Rejection.NONE);
  }

  private static boolean hasExternalCaller(accela.ir.Module module, Function target) {
    return module.getFunctions().stream()
        .filter(function -> function != target)
        .flatMap(function -> function.getBlocks().stream())
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> isCallTo(instruction, target));
  }

  private static boolean isCallTo(Instruction instruction, Function target) {
    return instruction.getOpcode() == Instruction.Opcode.CALL
        && instruction.getCallee() == target;
  }
}
