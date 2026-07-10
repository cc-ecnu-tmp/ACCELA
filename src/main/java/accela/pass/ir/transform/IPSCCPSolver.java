package accela.pass.ir.transform;

import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.pass.ir.transform.SCCP.ConstVal;
import accela.pass.ir.transform.SCCP.SCCPFact;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Solves executable functions, arguments, and returns to a joint SCCP fixed point. */
final class IPSCCPSolver {
  private final Module module;
  private final Set<Function> executable = Collections.newSetFromMap(new IdentityHashMap<>());
  private final Map<Function, List<ConstVal>> arguments = new IdentityHashMap<>();
  private final Map<Function, ConstVal> returns = new IdentityHashMap<>();
  private boolean changed;
  IPSCCPSolver(Module module) {
    this.module = module;
    for (Function function : module.getFunctions()) {
      arguments.put(
          function,
          new ArrayList<>(Collections.nCopies(function.getNumArgs(), ConstVal.BOT)));
      returns.put(function, ConstVal.BOT);
    }
  }

  boolean solve() {
    Function main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main"))
        .findFirst()
        .orElse(null);
    if (main == null) return false;
    executable.add(main);
    for (int i = 0; i < main.getNumArgs(); i++) joinArgument(main, i, ConstVal.TOP);

    do {
      changed = false;
      for (Function function : new ArrayList<>(executable)) analyze(function);
    } while (changed);

    boolean rewritten = false;
    for (Function function : new ArrayList<>(executable)) {
      SCCP.Analysis analysis =
          SCCP.analyze(function, entryFact(function), this::resolveKnownCall);
      rewritten |= SCCP.applyTransformations(function, analysis.result, analysis.transfer);
    }
    return rewritten;
  }

  private void analyze(Function function) {
    SCCP.Analysis analysis = SCCP.analyze(function, entryFact(function), this::resolveCall);
    ConstVal old = returns.get(function);
    ConstVal joined = ConstVal.join(old, SCCP.returnedValue(analysis));
    if (!old.equals(joined)) {
      returns.put(function, joined);
      changed = true;
    }
  }

  private SCCPFact entryFact(Function function) {
    SCCPFact fact = new SCCPFact();
    for (int i = 0; i < function.getNumArgs(); i++) {
      fact = fact.with(function.getArguments().get(i), arguments.get(function).get(i));
    }
    return fact;
  }

  private ConstVal resolveCall(Instruction call, SCCPFact fact) {
    Function callee = call.getCallee();
    if (callee == null || callee.getModule() != module) return ConstVal.TOP;
    if (executable.add(callee)) changed = true;
    if (call.getNumOperands() != callee.getNumArgs()) return ConstVal.TOP;
    for (int i = 0; i < callee.getNumArgs(); i++) {
      joinArgument(callee, i, fact.get(call.getOperand(i)));
    }
    return returns.get(callee);
  }

  private ConstVal resolveKnownCall(Instruction call, SCCPFact fact) {
    Function callee = call.getCallee();
    return callee != null && callee.getModule() == module
        ? returns.get(callee) : ConstVal.TOP;
  }

  private void joinArgument(Function function, int index, ConstVal value) {
    List<ConstVal> values = arguments.get(function);
    ConstVal old = values.get(index);
    ConstVal joined = ConstVal.join(old, value);
    if (!old.equals(joined)) {
      values.set(index, joined);
      changed = true;
    }
  }
}
