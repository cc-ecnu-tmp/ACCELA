package accela.pass.ir.transform.smallfunctioninliner;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.FunctionCloner;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.IdentityHashMap;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Inlines small, straight-line, non-recursive leaf functions. */
public final class SmallFunctionInliner {
  private static final int MAX_BODY_INSTRUCTIONS = 12;
  private static final int MAX_INLINE_SITES = 64;

  private SmallFunctionInliner() {}

  public static boolean runOnModule(accela.ir.Module module) {
    Map<Function, Integer> callCounts = countCalls(module);
    Set<Function> eligible = Collections.newSetFromMap(new IdentityHashMap<>());
    for (Function function : module.getFunctions()) {
      if (isEligibleCallee(function)
          && (callCounts.getOrDefault(function, 0) == 1
              || function.getEntryBlock().getInstructions().size() - 1 <= 3
                  && isReadOnly(function))) {
        eligible.add(function);
      }
    }
    int inlined = 0;
    for (Function caller : List.copyOf(module.getFunctions())) {
      for (BasicBlock block : List.copyOf(caller.getBlocks())) {
        for (Instruction call : List.copyOf(block.getInstructions())) {
          if (inlined == MAX_INLINE_SITES) return true;
          if (!isCandidate(module, caller, call, eligible)) continue;
          inline(call);
          inlined++;
        }
      }
    }
    return inlined != 0;
  }

  private static boolean isCandidate(
      accela.ir.Module module,
      Function caller,
      Instruction call,
      Set<Function> eligible) {
    if (call.getOpcode() != Instruction.Opcode.CALL) return false;
    Function callee = call.getCallee();
    return callee != null && callee != caller && callee.getModule() == module
        && eligible.contains(callee) && callee.getNumArgs() == call.getNumOperands();
  }

  private static boolean isEligibleCallee(Function callee) {
    if (callee.getBlocks().size() != 1) return false;
    List<Instruction> body = callee.getEntryBlock().getInstructions();
    if (body.isEmpty() || body.size() - 1 > MAX_BODY_INSTRUCTIONS) return false;
    Instruction ret = body.get(body.size() - 1);
    if (ret.getOpcode() != Instruction.Opcode.RET
        || ret.getNumOperands() != (callee.getReturnType() == accela.ir.Type.VOID ? 0 : 1)) return false;
    for (int index = 0; index + 1 < body.size(); index++) {
      Instruction.Opcode opcode = body.get(index).getOpcode();
      if (opcode == Instruction.Opcode.CALL || opcode == Instruction.Opcode.PHI
          || opcode == Instruction.Opcode.ALLOCA || opcode.isTerminator()) return false;
    }
    return true;
  }

  private static Map<Function, Integer> countCalls(accela.ir.Module module) {
    Map<Function, Integer> counts = new IdentityHashMap<>();
    for (Function function : module.getFunctions()) {
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (instruction.getOpcode() == Instruction.Opcode.CALL
              && instruction.getCallee() != null) counts.merge(instruction.getCallee(), 1, Integer::sum);
        }
      }
    }
    return counts;
  }

  private static boolean isReadOnly(Function function) {
    return function.getEntryBlock().getInstructions().stream()
        .noneMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.STORE);
  }

  private static void inline(Instruction call) {
    Function callee = call.getCallee();
    List<Instruction> body = callee.getEntryBlock().getInstructions();
    Map<Value, Value> values = new IdentityHashMap<>();
    for (int index = 0; index < callee.getNumArgs(); index++) {
      values.put(callee.getArguments().get(index), call.getOperand(index));
    }
    for (int index = 0; index + 1 < body.size(); index++) {
      Instruction source = body.get(index);
      Instruction clone = FunctionCloner.cloneInstruction(source);
      for (int operand = 0; operand < source.getNumOperands(); operand++) {
        Value value = source.getOperand(operand);
        clone.addOperand(values.getOrDefault(value, value));
      }
      call.getParent().insertInstructionBefore(call, clone);
      values.put(source, clone);
    }
    Instruction ret = body.get(body.size() - 1);
    if (ret.getNumOperands() == 1) {
      Value result = ret.getOperand(0);
      call.replaceAllUsesWith(values.getOrDefault(result, result));
    }
    call.eraseFromParent();
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
