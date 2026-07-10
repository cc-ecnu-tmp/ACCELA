package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Eliminates repeated calls to internal functions that cannot access memory. */
public final class ReadNoneCallCSE {
  private ReadNoneCallCSE() {}

  static boolean runOnModule(accela.ir.Module module) {
    Set<Function> readNone = findReadNoneFunctions(module);
    boolean changed = false;
    for (Function function : module.getFunctions()) {
      for (BasicBlock block : function.getBlocks()) {
        Map<CallKey, Instruction> available = new HashMap<>();
        for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
          if (instruction.getOpcode() != Instruction.Opcode.CALL
              || !readNone.contains(instruction.getCallee())
              || !instruction.hasResult()) continue;
          List<Value> arguments = new ArrayList<>();
          for (int i = 0; i < instruction.getNumOperands(); i++) {
            arguments.add(instruction.getOperand(i));
          }
          CallKey key = new CallKey(instruction.getCallee(), List.copyOf(arguments));
          Instruction existing = available.putIfAbsent(key, instruction);
          if (existing == null) continue;
          instruction.replaceAllUsesWith(existing);
          instruction.eraseFromParent();
          changed = true;
        }
      }
    }
    return changed;
  }

  private static Set<Function> findReadNoneFunctions(accela.ir.Module module) {
    Set<Function> impure = Collections.newSetFromMap(new IdentityHashMap<>());
    for (Function function : module.getFunctions()) {
      if (hasDirectMemoryEffect(function, module)) impure.add(function);
    }
    boolean changed;
    do {
      changed = false;
      for (Function function : module.getFunctions()) {
        if (impure.contains(function) || !callsAny(function, impure)) continue;
        impure.add(function);
        changed = true;
      }
    } while (changed);
    Set<Function> result = Collections.newSetFromMap(new IdentityHashMap<>());
    result.addAll(module.getFunctions());
    result.removeAll(impure);
    return result;
  }

  private static boolean hasDirectMemoryEffect(Function function, accela.ir.Module module) {
    return instructions(function).anyMatch(instruction ->
        instruction.getOpcode() == Instruction.Opcode.LOAD
            || instruction.getOpcode() == Instruction.Opcode.STORE
            || instruction.getOpcode() == Instruction.Opcode.CALL
                && (instruction.getCallee() == null
                    || instruction.getCallee().getModule() != module));
  }

  private static boolean callsAny(Function function, Set<Function> callees) {
    return instructions(function).anyMatch(instruction ->
        instruction.getOpcode() == Instruction.Opcode.CALL
            && callees.contains(instruction.getCallee()));
  }

  private static java.util.stream.Stream<Instruction> instructions(Function function) {
    return function.getBlocks().stream().flatMap(block -> block.getInstructions().stream());
  }

  private record CallKey(Function callee, List<Value> arguments) {}

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
