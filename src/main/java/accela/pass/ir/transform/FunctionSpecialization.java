package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.FunctionCloner;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Clones profitable constant-argument call groups before IPSCCP. */
public final class FunctionSpecialization {
  private static final int MAX_CLONES = 3;
  private static final int MIN_SAVINGS_PERCENT = 20;
  private static final int MAX_CODE_GROWTH = 3;
  private FunctionSpecialization() {}

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        Module module, ModuleAnalysisManager mam, FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  static boolean runOnModule(Module module) {
    boolean changed = false;
    for (var entry : collectCallSites(module).entrySet()) {
      Function source = entry.getKey();
      List<Instruction> calls = entry.getValue();
      int sourceSize = instructionCount(source);
      if (sourceSize == 0 || isDirectlyRecursive(source)) continue;

      Map<SpecializationSignature, List<Instruction>> groups = new LinkedHashMap<>();
      for (Instruction call : calls) {
        SpecializationSignature signature = SpecializationSignature.fromCall(call);
        if (!signature.isEmpty()) {
          groups.computeIfAbsent(signature, ignored -> new ArrayList<>()).add(call);
        }
      }
      if (groups.size() == 1 && groups.values().iterator().next().size() == calls.size()) continue;

      int clones = 0;
      int growth = 0;
      for (var group : groups.entrySet()) {
        if (clones == MAX_CLONES) break;
        Function clone = FunctionCloner.cloneFunction(
            source, source.getName() + ".specialized." + (clones + 1));
        group.getKey().applyTo(clone);
        optimize(clone);
        int cloneSize = instructionCount(clone);
        int savings = sourceSize - cloneSize;
        if (savings * 100 < sourceSize * MIN_SAVINGS_PERCENT) continue;
        if (growth + cloneSize > sourceSize * MAX_CODE_GROWTH) continue;

        module.addFunction(clone);
        for (Instruction call : group.getValue()) call.setCallee(clone);
        growth += cloneSize;
        clones++;
        changed = true;
      }
    }
    return changed;
  }
  private static Map<Function, List<Instruction>> collectCallSites(Module module) {
    Map<Function, List<Instruction>> calls = new LinkedHashMap<>();
    for (Function caller : module.getFunctions()) {
      for (BasicBlock block : caller.getBlocks()) {
        for (Instruction instruction : block.getInstructions()) {
          Function callee = instruction.getCallee();
          if (instruction.getOpcode() == Instruction.Opcode.CALL
              && callee != null && callee.getModule() == module) {
            calls.computeIfAbsent(callee, ignored -> new ArrayList<>()).add(instruction);
          }
        }
      }
    }
    return calls;
  }
  private static boolean isDirectlyRecursive(Function function) {
    return function.getBlocks().stream().flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getCallee() == function);
  }
  private static void optimize(Function function) {
    SCCP.runOnFunction(function);
    InstSimplify.runOnFunction(function);
    ADCE.runOnFunction(function);
    SimplifyCFG.runOnFunction(function);
  }

  private static int instructionCount(Function function) {
    return function.getBlocks().stream().mapToInt(block -> block.getInstructions().size()).sum();
  }
}
