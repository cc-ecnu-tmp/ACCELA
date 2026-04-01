package accela.pass.ir.instrument;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import java.util.EnumMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.StringJoiner;

/**
 * A simple framework for benchmarking
 */
public final class IRMetrics {
  private int functions;
  private int blocks;
  private int instructions;
  private int allocas;
  private int aggregateAllocas;
  private int scalarAllocas;
  private int loads;
  private int stores;
  private int geps;
  private int phis;
  private int calls;
  private int branches;
  private int returns;
  private final EnumMap<Instruction.Opcode, Integer> opcodeCounts =
      new EnumMap<>(Instruction.Opcode.class);

  public static IRMetrics capture(accela.ir.Module module) {
    IRMetrics metrics = new IRMetrics();
    for (Function function : module.getFunctions()) {
      metrics.addFunction(function);
    }
    return metrics;
  }

  public static IRMetrics capture(Function function) {
    IRMetrics metrics = new IRMetrics();
    metrics.addFunction(function);
    return metrics;
  }

  private void addFunction(Function function) {
    functions++;
    for (BasicBlock block : function.getBlocks()) {
      blocks++;
      for (Instruction inst : block.getInstructions()) {
        addInstruction(inst);
      }
    }
  }

  private void addInstruction(Instruction inst) {
    instructions++;
    opcodeCounts.merge(inst.getOpcode(), 1, Integer::sum);
    switch (inst.getOpcode()) {
      case ALLOCA:
        allocas++;
        Type allocType = inst.getAllocatedType();
        if (allocType != null && allocType.isArray()) aggregateAllocas++;
        else scalarAllocas++;
        break;
      case LOAD:
        loads++;
        break;
      case STORE:
        stores++;
        break;
      case GEP:
        geps++;
        break;
      case PHI:
        phis++;
        break;
      case CALL:
        calls++;
        break;
      case BR:
      case CONDBR:
        branches++;
        break;
      case RET:
        returns++;
        break;
      default:
        break;
    }
  }

  public String diffSummary(IRMetrics after) {
    Map<String, int[]> changed = new LinkedHashMap<>();
    addIfChanged(changed, "func", functions, after.functions);
    addIfChanged(changed, "block", blocks, after.blocks);
    addIfChanged(changed, "inst", instructions, after.instructions);
    addIfChanged(changed, "alloca", allocas, after.allocas);
    addIfChanged(changed, "agg_alloca", aggregateAllocas, after.aggregateAllocas);
    addIfChanged(changed, "scalar_alloca", scalarAllocas, after.scalarAllocas);
    addIfChanged(changed, "load", loads, after.loads);
    addIfChanged(changed, "store", stores, after.stores);
    addIfChanged(changed, "gep", geps, after.geps);
    addIfChanged(changed, "phi", phis, after.phis);
    addIfChanged(changed, "call", calls, after.calls);
    addIfChanged(changed, "branch", branches, after.branches);
    addIfChanged(changed, "ret", returns, after.returns);

    for (Instruction.Opcode opcode : Instruction.Opcode.values()) {
      int beforeCount = opcodeCounts.getOrDefault(opcode, 0);
      int afterCount = after.opcodeCounts.getOrDefault(opcode, 0);
      if (beforeCount != afterCount && !isAlreadySummarized(opcode)) {
        addIfChanged(changed, opcode.name().toLowerCase(), beforeCount, afterCount);
      }
    }

    if (changed.isEmpty()) return "no structural change";

    StringJoiner joiner = new StringJoiner(", ");
    for (Map.Entry<String, int[]> entry : changed.entrySet()) {
      int before = entry.getValue()[0];
      int afterValue = entry.getValue()[1];
      int delta = afterValue - before;
      joiner.add(entry.getKey() + " " + before + "->" + afterValue + formatDelta(delta));
    }
    return joiner.toString();
  }

  private static void addIfChanged(Map<String, int[]> changed, String name, int before, int after) {
    if (before != after) {
      changed.put(name, new int[] {before, after});
    }
  }

  private static String formatDelta(int delta) {
    if (delta == 0) return " (0)";
    return delta > 0 ? " (+" + delta + ")" : " (" + delta + ")";
  }

  private static boolean isAlreadySummarized(Instruction.Opcode opcode) {
    return switch (opcode) {
      case ALLOCA, LOAD, STORE, GEP, PHI, CALL, BR, CONDBR, RET -> true;
      default -> false;
    };
  }
}
