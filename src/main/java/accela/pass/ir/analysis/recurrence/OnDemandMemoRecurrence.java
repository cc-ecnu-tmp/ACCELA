package accela.pass.ir.analysis.recurrence;

import accela.ir.Function;
import accela.ir.Instruction;
import java.util.List;
import java.util.Objects;

/** A pure two-dimensional direct recurrence suitable for bounded on-demand memoization. */
public record OnDemandMemoRecurrence(
    Function function,
    List<Instruction> recursiveCalls,
    List<Transition> transitions,
    DomainShape domainShape) {

  /** Runtime domain proved closed under the admitted recursive transitions. */
  public enum DomainShape {
    RECTANGULAR_NONNEGATIVE,
    TRIANGULAR_NONNEGATIVE
  }

  /** Per-component decrease amounts for one recursive edge; zero means that component is unchanged. */
  public record Transition(int firstDecrease, int secondDecrease) {
    public Transition {
      if (firstDecrease < 0 || secondDecrease < 0
          || firstDecrease == 0 && secondDecrease == 0) {
        throw new IllegalArgumentException(
            "a recurrence transition must strictly decrease at least one component");
      }
    }
  }

  public OnDemandMemoRecurrence {
    Objects.requireNonNull(function, "function");
    Objects.requireNonNull(domainShape, "domainShape");
    recursiveCalls = List.copyOf(recursiveCalls);
    transitions = List.copyOf(transitions);
    if (function.getNumArgs() != 2) {
      throw new IllegalArgumentException("RRT2 recurrence must have exactly two arguments");
    }
    if (recursiveCalls.size() != transitions.size() || recursiveCalls.isEmpty()) {
      throw new IllegalArgumentException("recursive calls and transitions must be non-empty and paired");
    }
  }
}
