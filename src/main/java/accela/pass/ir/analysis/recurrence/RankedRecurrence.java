package accela.pass.ir.analysis.recurrence;

import accela.ir.Function;
import accela.ir.Instruction;
import java.util.ArrayList;
import java.util.List;

/** A pure finite recurrence whose recursive edges strictly lower one integer rank. */
public record RankedRecurrence(
    Function function,
    int rankArgument,
    List<Integer> stateArguments,
    int rankLimit,
    List<Instruction> recursiveCalls) {

  public RankedRecurrence {
    stateArguments = List.copyOf(stateArguments);
    recursiveCalls = List.copyOf(recursiveCalls);
  }

  public List<Integer> domainArguments() {
    List<Integer> arguments = new ArrayList<>();
    arguments.add(rankArgument);
    arguments.addAll(stateArguments);
    return List.copyOf(arguments);
  }
}
