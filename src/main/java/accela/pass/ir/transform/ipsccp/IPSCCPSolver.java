package accela.pass.ir.transform;

import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.pass.ir.transform.SCCP.ConstVal;
import accela.pass.ir.transform.SCCP.SCCPFact;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** IPSCCP fixed-point solver for executable functions, arguments, and returns. */
final class IPSCCPSolver {
  private static final class FunctionState {
    final ArrayList<ConstVal> arguments;
    ConstVal returned = ConstVal.BOT;
    FunctionState(int argumentCount) {
      arguments = new ArrayList<>(Collections.nCopies(argumentCount, ConstVal.BOT));
    }
  }

  private final Function main;
  private final Set<Function> executable = Collections.newSetFromMap(new IdentityHashMap<>());
  private final IdentityHashMap<Function, FunctionState> states = new IdentityHashMap<>();
  private boolean latticeChanged;

  IPSCCPSolver(Module module) {
    for (Function function : module.getFunctions())
      states.put(function, new FunctionState(function.getNumArgs()));
    main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main")).findFirst().orElse(null);
  }

  boolean solve() {
    if (main == null) return false;
    executable.add(main);
    Collections.fill(states.get(main).arguments, ConstVal.TOP);
    solveLattices();

    boolean rewritten = false;
    for (Function function : new ArrayList<>(executable)) rewritten |= rewrite(function);
    return rewritten;
  }

  private void solveLattices() {
    do {
      latticeChanged = false;
      for (Function function : new ArrayList<>(executable)) analyze(function);
    } while (latticeChanged);
  }

  private void analyze(Function function) {
    SCCP.Analysis analysis = SCCP.analyze(function, entryFact(function), this::resolveCall);
    FunctionState state = states.get(function);
    ConstVal returned = ConstVal.join(state.returned, SCCP.returnedValue(analysis));
    if (state.returned.equals(returned)) return;
    state.returned = returned;
    latticeChanged = true;
  }

  private boolean rewrite(Function function) {
    SCCP.Analysis analysis = SCCP.analyze(function, entryFact(function), this::resolveKnownCall);
    boolean changed = SCCP.applyTransformations(function, analysis.result, analysis.transfer);
    return SimplifyCFG.runOnFunction(function) || changed;
  }

  private SCCPFact entryFact(Function function) {
    SCCPFact fact = new SCCPFact();
    var arguments = states.get(function).arguments;
    for (int i = 0; i < function.getNumArgs(); i++)
      fact = fact.with(function.getArguments().get(i), arguments.get(i));
    return fact;
  }

  private ConstVal resolveCall(Instruction call, SCCPFact fact) {
    Function callee = call.getCallee();
    FunctionState state = states.get(callee);
    if (state == null) return ConstVal.TOP;
    if (executable.add(callee)) latticeChanged = true;
    if (call.getNumOperands() != callee.getNumArgs()) return ConstVal.TOP;
    for (int i = 0; i < callee.getNumArgs(); i++)
      joinArgument(state, i, fact.get(call.getOperand(i)));
    return state.returned;
  }

  private ConstVal resolveKnownCall(Instruction call, SCCPFact ignored) {
    FunctionState state = states.get(call.getCallee());
    return state == null ? ConstVal.TOP : state.returned;
  }

  private void joinArgument(FunctionState state, int index, ConstVal value) {
    ConstVal old = state.arguments.get(index);
    ConstVal joined = ConstVal.join(old, value);
    if (!old.equals(joined)) {
      state.arguments.set(index, joined);
      latticeChanged = true;
    }
  }
}
