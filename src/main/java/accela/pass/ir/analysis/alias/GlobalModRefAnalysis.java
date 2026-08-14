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
    if (isDefinition(module, callee)) return effects.get(callee);
    MemoryEffects external = new MemoryEffects();
    String name = callee == null ? "" : callee.getName();
    switch (name) {
      case "getarray", "getfarray" -> {
        external.writtenArguments.set(0);
        external.observable = true;
      }
      case "putarray", "putfarray" -> {
        external.readArguments.set(1);
        external.observable = true;
      }
      case "getint", "getch", "getfloat", "putint", "putch", "putfloat",
          "_sysy_starttime", "_sysy_stoptime" -> external.observable = true;
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

    /** True when a direct internal call is deterministic and has no observable effects. */
    public boolean isPure(Instruction call) {
      Function callee = call.getCallee();
      return isDefinition(module, callee) && functionEffects.get(callee).isPure();
    }

    public FunctionMemorySummary summary(Function function) {
      if (function == null || function.getModule() != module) {
        throw new IllegalArgumentException("function is not part of the analyzed module");
      }
      return FunctionMemorySummary.forFunction(this, function);
    }

    boolean readsArgument(Function function, int index) {
      return effects(function).readArguments.get(index);
    }

    boolean writesArgument(Function function, int index) {
      return effects(function).writtenArguments.get(index);
    }

    boolean escapesArgument(Function function, int index) {
      return effects(function).escapedArguments.get(index);
    }

    boolean hasUnknownEffects(Function function) {
      MemoryEffects effects = effects(function);
      return effects.unknownRead || effects.unknownWrite;
    }

    private MemoryEffects effects(Function function) {
      MemoryEffects result = functionEffects.get(function);
      if (result == null) throw new IllegalArgumentException("unknown function in memory summary");
      return result;
    }

    private MemoryEffects callEffects(Instruction call) {
      return effectsForCall(module, functionEffects, call);
    }
  }

  private static boolean isDefinition(accela.ir.Module module, Function function) {
    return function != null && function.getModule() == module && !function.getBlocks().isEmpty();
  }
}
