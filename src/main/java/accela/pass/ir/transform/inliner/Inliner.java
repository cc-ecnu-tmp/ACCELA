package accela.pass.ir.transform.inliner;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Inlines profitable direct calls using a bounded, bottom-up cost model.
 *
 * <p>SysY has only direct, fixed-signature calls, so the standard inlining transform does not need
 * devirtualization, varargs, exceptions, or operand-bundle handling. Recursive callees may be
 * exposed once in a non-recursive caller, but recursive call-graph edges are never inlined.
 */
public final class Inliner {
  private static final int MAX_ROUNDS = 4;
  private static final int MAX_INLINE_SITES = 128;
  private static final int MAX_SINGLE_USE_COST = 240;
  private static final int MAX_REPEATED_COST = 80;
  private static final int MAX_CALLER_INSTRUCTIONS = 2000;

  private Inliner() {}

  static boolean runOnModule(accela.ir.Module module) {
    Map<Function, Set<Function>> exposedRecursiveCallees = new IdentityHashMap<>();
    int inlined = 0;
    for (int round = 0; round < MAX_ROUNDS && inlined < MAX_INLINE_SITES; round++) {
      Map<Function, Integer> callCounts = countCalls(module);
      boolean changedThisRound = false;
      for (Function caller : List.copyOf(module.getFunctions())) {
        for (BasicBlock block : List.copyOf(caller.getBlocks())) {
          for (Instruction call : List.copyOf(block.getInstructions())) {
            Function callee = call.getCallee();
            if (!shouldInline(module, caller, call, callCounts)) continue;
            boolean recursive = reaches(callee, callee);
            Set<Function> exposed =
                exposedRecursiveCallees.computeIfAbsent(
                    caller, ignored -> Collections.newSetFromMap(new IdentityHashMap<>()));
            if (recursive && !exposed.add(callee)) continue;
            InlineFunction.inline(call);
            changedThisRound = true;
            if (++inlined == MAX_INLINE_SITES) return true;
          }
        }
      }
      if (!changedThisRound) break;
    }
    return inlined != 0;
  }

  private static boolean shouldInline(
      accela.ir.Module module,
      Function caller,
      Instruction call,
      Map<Function, Integer> callCounts) {
    if (call.getOpcode() != Instruction.Opcode.CALL) return false;
    Function callee = call.getCallee();
    if (callee == null
        || callee == caller
        || callee.getModule() != module
        || callee.getNumArgs() != call.getNumOperands()
        || callee.getBlocks().isEmpty()
        || reaches(callee, caller)) {
      return false;
    }

    int cost = instructionCount(callee);
    int threshold =
        callCounts.getOrDefault(callee, 0) == 1 ? MAX_SINGLE_USE_COST : MAX_REPEATED_COST;
    return cost <= threshold
        && instructionCount(caller) + cost <= MAX_CALLER_INSTRUCTIONS
        && hasValidReturns(callee);
  }

  private static boolean hasValidReturns(Function function) {
    boolean hasReturn = false;
    for (BasicBlock block : function.getBlocks()) {
      Instruction terminator = block.getTerminator();
      if (terminator == null) return false;
      if (terminator.getOpcode() == Instruction.Opcode.RET) hasReturn = true;
    }
    return function.getReturnType() == accela.ir.Type.VOID || hasReturn;
  }

  private static int instructionCount(Function function) {
    int count = 0;
    for (BasicBlock block : function.getBlocks()) count += block.getInstructions().size();
    return count;
  }

  private static Map<Function, Integer> countCalls(accela.ir.Module module) {
    Map<Function, Integer> counts = new IdentityHashMap<>();
    for (Function function : module.getFunctions()) {
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() == Instruction.Opcode.CALL
              && instruction.getCallee() != null
              && instruction.getCallee().getModule() == module) {
            counts.merge(instruction.getCallee(), 1, Integer::sum);
          }
        }
      }
    }
    return counts;
  }

  /** Returns whether a non-empty direct-call path from {@code start} reaches {@code target}. */
  private static boolean reaches(Function start, Function target) {
    Set<Function> visited = Collections.newSetFromMap(new IdentityHashMap<>());
    ArrayDeque<Function> worklist = new ArrayDeque<>();
    worklist.add(start);
    while (!worklist.isEmpty()) {
      Function function = worklist.removeFirst();
      if (!visited.add(function)) continue;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() != Instruction.Opcode.CALL) continue;
          Function callee = instruction.getCallee();
          if (callee == target) return true;
          if (callee != null && callee.getModule() == start.getModule()) {
            worklist.addLast(callee);
          }
        }
      }
    }
    return false;
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
