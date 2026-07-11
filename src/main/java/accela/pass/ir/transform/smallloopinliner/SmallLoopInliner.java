package accela.pass.ir.transform.smallloopinliner;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Inlines uniquely called, leaf functions containing one small loop. */
public final class SmallLoopInliner {
  private static final int MAX_BLOCKS = 24;
  private static final int MAX_INSTRUCTIONS = 200;

  private SmallLoopInliner() {}

  static boolean runOnModule(accela.ir.Module module) {
    Map<Function, Integer> calls = countCalls(module);
    Set<Function> eligible = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    for (Function function : module.getFunctions()) {
      if (calls.getOrDefault(function, 0) == 1 && isEligible(function)) {
        eligible.add(function);
      }
    }
    boolean changed = false;
    for (Function caller : List.copyOf(module.getFunctions())) {
      for (BasicBlock block : List.copyOf(caller.getBlocks())) {
        for (Instruction call : List.copyOf(block.getInstructions())) {
          if (call.getOpcode() != Instruction.Opcode.CALL
              || !eligible.contains(call.getCallee())
              || call.getCallee() == caller) continue;
          CFGInliner.inline(call);
          changed = true;
        }
      }
    }
    return changed;
  }

  private static boolean isEligible(Function function) {
    if (function.getBlocks().size() < 2 || function.getBlocks().size() > MAX_BLOCKS
        || !hasBackedge(function)) return false;
    int instructions = 0;
    for (BasicBlock block : function.getBlocks()) {
      if (block.getTerminator() == null) return false;
      for (Instruction instruction : block.getInstructions()) {
        instructions++;
        if (instruction.getOpcode() == Instruction.Opcode.CALL
            || instruction.getOpcode() == Instruction.Opcode.ALLOCA) return false;
      }
    }
    return instructions <= MAX_INSTRUCTIONS;
  }

  private static boolean hasBackedge(Function function) {
    Map<BasicBlock, Integer> order = new IdentityHashMap<>();
    for (int index = 0; index < function.getBlocks().size(); index++) {
      order.put(function.getBlocks().get(index), index);
    }
    for (BasicBlock block : function.getBlocks()) {
      for (BasicBlock successor : block.getSuccessors()) {
        if (order.get(successor) <= order.get(block)) return true;
      }
    }
    return false;
  }

  private static Map<Function, Integer> countCalls(accela.ir.Module module) {
    Map<Function, Integer> counts = new IdentityHashMap<>();
    for (Function function : module.getFunctions()) {
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() == Instruction.Opcode.CALL
              && instruction.getCallee() != null) {
            counts.merge(instruction.getCallee(), 1, Integer::sum);
          }
        }
      }
    }
    return counts;
  }

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
