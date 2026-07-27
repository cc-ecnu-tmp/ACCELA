package accela.pass.ir.transform;

import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Passes write-once scalar globals through the call graph instead of memory. */
final class GlobalScalarParameterPromotion {
  private GlobalScalarParameterPromotion() {}

  static boolean runOnModule(accela.ir.Module module) {
    Function main = module.getFunctions().stream()
        .filter(function -> function.getName().equals("main"))
        .findFirst()
        .orElse(null);
    if (main == null || main.getEntryBlock() == null) return false;

    GlobalModRefAnalysis.Result modRef = GlobalModRefAnalysis.analyze(module);
    boolean changed = false;
    for (GlobalVariable global : List.copyOf(module.getGlobals())) {
      Plan plan = plan(module, main, global, modRef);
      if (plan == null) continue;
      apply(plan);
      changed = true;
    }
    return changed;
  }

  private static Plan plan(
      accela.ir.Module module,
      Function main,
      GlobalVariable global,
      GlobalModRefAnalysis.Result modRef) {
    Type type = global.getValueType();
    if (type.isArray() || type.isPointer()) return null;

    Instruction store = null;
    Set<Function> consumers = identitySet();
    List<Instruction> loads = new ArrayList<>();
    for (Use use : List.copyOf(global.getUses())) {
      Instruction user = use.getUser();
      if (user.getParent() == null) return null;
      Function owner = user.getParent().getParent();
      if (user.getOpcode() == Instruction.Opcode.LOAD && use.getOperandIndex() == 0) {
        loads.add(user);
        consumers.add(owner);
      } else if (user.getOpcode() == Instruction.Opcode.STORE
          && use.getOperandIndex() == 1 && store == null) {
        store = user;
      } else {
        return null;
      }
    }
    if (store == null
        || store.getParent() != main.getEntryBlock()
        || hasReadBefore(store, global, modRef)) return null;

    List<Instruction> calls = callsIn(module);
    boolean grew;
    do {
      grew = false;
      for (Instruction call : calls) {
        if (consumers.contains(call.getCallee())) {
          grew |= consumers.add(call.getParent().getParent());
        }
      }
    } while (grew);
    consumers.remove(main);
    return new Plan(main, global, store, loads, consumers, calls);
  }

  private static boolean hasReadBefore(
      Instruction store,
      GlobalVariable global,
      GlobalModRefAnalysis.Result modRef) {
    for (Instruction instruction : store.getParent().getInstructions()) {
      if (instruction == store) return false;
      if ((instruction.getOpcode() == Instruction.Opcode.CALL
              && modRef.mayRead(instruction, global))
          || (instruction.getOpcode() == Instruction.Opcode.LOAD
              && instruction.getOperand(0) == global)) return true;
    }
    return true;
  }

  private static void apply(Plan plan) {
    Value initialValue = plan.store.getOperand(0);
    Map<Function, Value> parameters = new IdentityHashMap<>();
    for (Function function : plan.consumers) {
      parameters.put(
          function,
          function.addArgument(
              plan.global.getValueType(), "%" + plan.global.getName() + ".ssa"));
    }
    for (Instruction load : plan.loads) {
      Function owner = load.getParent().getParent();
      load.replaceAllUsesWith(
          owner == plan.main ? initialValue : parameters.get(owner));
      load.eraseFromParent();
    }
    for (Instruction call : plan.calls) {
      if (!plan.consumers.contains(call.getCallee())) continue;
      Function caller = call.getParent().getParent();
      call.addOperand(caller == plan.main ? initialValue : parameters.get(caller));
    }
    plan.store.eraseFromParent();
  }

  private static List<Instruction> callsIn(accela.ir.Module module) {
    return module.getFunctions().stream()
        .flatMap(function -> function.getBlocks().stream())
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL)
        .toList();
  }

  private static Set<Function> identitySet() {
    return Collections.newSetFromMap(new IdentityHashMap<>());
  }

  private record Plan(
      Function main,
      GlobalVariable global,
      Instruction store,
      List<Instruction> loads,
      Set<Function> consumers,
      List<Instruction> calls) {}

}
