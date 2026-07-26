package accela.pass.ir.analysis.alias;

import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import java.util.IdentityHashMap;
import java.util.Map;

/** Computes transitive memory effects for direct calls in one whole-program module. */
public final class GlobalModRefAnalysis {
  private GlobalModRefAnalysis() {}

  public static Result analyze(accela.ir.Module module) {
    Map<Function, MemoryEffects> effects = new IdentityHashMap<>();
    for (Function function : module.getFunctions()) {
      effects.put(function, collectDirectEffects(function));
    }
    boolean changed;
    do {
      changed = false;
      for (Function function : module.getFunctions()) {
        for (var block : function.getBlocks()) {
          for (Instruction instruction : block.getInstructions()) {
            if (instruction.getOpcode() != Instruction.Opcode.CALL) continue;
            changed |= effects.get(function).mergeCall(
                effectsForCall(module, effects, instruction), instruction, function);
          }
        }
      }
    } while (changed);
    return new Result(module, effects);
  }

  private static MemoryEffects collectDirectEffects(Function function) {
    MemoryEffects effects = new MemoryEffects();
    for (var block : function.getBlocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
          effects.addAccess(instruction.getOperand(0), function, false);
        } else if (instruction.getOpcode() == Instruction.Opcode.STORE) {
          effects.addAccess(instruction.getOperand(1), function, true);
        }
      }
    }
    return effects;
  }

  private static MemoryEffects effectsForCall(
      accela.ir.Module module,
      Map<Function, MemoryEffects> effects,
      Instruction call) {
    Function callee = call.getCallee();
    if (callee != null && callee.getModule() == module) return effects.get(callee);
    MemoryEffects external = new MemoryEffects();
    String name = callee == null ? "" : callee.getName();
    switch (name) {
      case "getarray", "getfarray" -> external.writtenArguments.set(0);
      case "putarray", "putfarray" -> external.readArguments.set(1);
      case "getint", "getch", "getfloat", "putint", "putch", "putfloat",
          "_sysy_starttime", "_sysy_stoptime" -> {}
      default -> {
        external.unknownRead = true;
        external.unknownWrite = true;
      }
    }
    return external;
  }

  public static final class Result {
    private final accela.ir.Module module;
    private final Map<Function, MemoryEffects> functionEffects;

    private Result(
        accela.ir.Module module, Map<Function, MemoryEffects> functionEffects) {
      this.module = module;
      this.functionEffects = functionEffects;
    }

    public boolean mayRead(Instruction call, Value pointer) {
      return callEffects(call).mayAccess(call, pointer, false);
    }

    public boolean mayWrite(Instruction call, Value pointer) {
      return callEffects(call).mayAccess(call, pointer, true);
    }

    private MemoryEffects callEffects(Instruction call) {
      return effectsForCall(module, functionEffects, call);
    }
  }
}
