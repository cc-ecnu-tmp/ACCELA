package accela.pass.ir.transform.specialize;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Value;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.alias.FunctionMemorySummary;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Bounded direct-call specialization for constant SysY scalar arguments. */
public final class FunctionSpecialization {
  private static final int MAX_CLONES_PER_FUNCTION = 2;
  private static final int MAX_MODULE_GROWTH_PERCENT = 15;

  private FunctionSpecialization() {}

  public static final class Pass implements ModulePass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;

    public Pass(PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = instrumentation;
      this.descriptor = descriptor;
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Module module, ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      PassDecisionEmitter decision = instrumentation.decisionEmitter(
          descriptor, occurrence, "module", "<module>");
      decision.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      int cloned = specialize(module);
      if (cloned > 0) {
        decision.applied(DecisionReasonCode.APPLIED_PROFITABLE);
        return PreservedAnalyses.none();
      }
      decision.rejectedLegality("candidate.function-specialization.constant-argument");
      return PreservedAnalyses.all();
    }

    private static int specialize(Module module) {
      GlobalModRefAnalysis.Result modRef = GlobalModRefAnalysis.analyze(module);
      int originalInstructions = instructionCount(module);
      int growthBudget = Math.max(1, originalInstructions * MAX_MODULE_GROWTH_PERCENT / 100);
      int growth = 0;
      int clones = 0;
      Map<Function, List<Specialization>> specializations = new IdentityHashMap<>();
      for (Function caller : List.copyOf(module.getFunctions())) {
        for (BasicBlock block : caller.getBlocks()) {
          for (Instruction call : List.copyOf(block.getInstructions())) {
            if (call.getOpcode() != Instruction.Opcode.CALL) continue;
            Function callee = call.getCallee();
            if (!eligible(module, caller, callee, call, modRef)) continue;
            Map<Integer, Constant> constants = constantArguments(call);
            if (constants.isEmpty()) continue;
            SpecializationKey key = new SpecializationKey(constants);
            List<Specialization> variants = specializations.computeIfAbsent(
                callee, ignored -> new ArrayList<>());
            Function specialized = variants.stream()
                .filter(variant -> variant.key.equals(key))
                .map(variant -> variant.function)
                .findFirst().orElse(null);
            if (specialized == null) {
              if (variants.size() >= MAX_CLONES_PER_FUNCTION) continue;
              int cost = instructionCount(callee);
              if (growth + cost > growthBudget) continue;
              String name = freshName(module, callee.getName() + ".spec." + variants.size());
              specialized = cloneFunction(module, callee, constants, name);
              variants.add(new Specialization(key, specialized));
              growth += cost;
              clones++;
            }
            call.setCallee(specialized);
          }
        }
      }
      return clones;
    }

    private static boolean eligible(Module module, Function caller, Function callee,
        Instruction call, GlobalModRefAnalysis.Result modRef) {
      if (callee == null
          || callee.getModule() != module
          || callee == caller
          || callee.getBlocks().isEmpty()
          || callee.getNumArgs() != call.getNumOperands()
          || reaches(callee, callee)
          || !hasReturn(callee)) return false;
      FunctionMemorySummary summary = modRef.summary(callee);
      if (summary.hasUnknownEffects()) return false;
      List<Value> pointerArguments = new ArrayList<>();
      for (int index = 0; index < callee.getNumArgs(); index++) {
        if (!callee.getArguments().get(index).getType().isPointer()) continue;
        if (!summary.isReadonlyArgument(index)) return false;
        Value actual = call.getOperand(index);
        if (!PointerProvenance.analyze(actual).exact()) return false;
        pointerArguments.add(actual);
      }
      for (int left = 0; left < pointerArguments.size(); left++) {
        for (int right = left + 1; right < pointerArguments.size(); right++) {
          if (PointerProvenance.mayAlias(pointerArguments.get(left), pointerArguments.get(right))) {
            return false;
          }
        }
      }
      return true;
    }

    private static Map<Integer, Constant> constantArguments(Instruction call) {
      Map<Integer, Constant> constants = new java.util.LinkedHashMap<>();
      for (int index = 0; index < call.getNumOperands(); index++) {
        Value value = call.getOperand(index);
        if (value instanceof Constant constant
            && (constant.getType().isInt() || constant.getType().isFloat())) {
          constants.put(index, constant);
        }
      }
      return constants;
    }

    private static Function cloneFunction(Module module, Function source,
        Map<Integer, Constant> constants, String name) {
      Function clone = new Function(name, source.getReturnType());
      Map<Value, Value> values = new IdentityHashMap<>();
      for (int index = 0; index < source.getNumArgs(); index++) {
        Function.Argument argument = source.getArguments().get(index);
        Function.Argument cloneArgument = clone.addArgument(argument.getType(), argument.getName());
        values.put(argument, constants.containsKey(index) ? constants.get(index) : cloneArgument);
      }
      for (BasicBlock sourceBlock : source.getBlocks()) {
        values.put(sourceBlock, clone.addBlock(sourceBlock.getLabel() + ".spec"));
      }
      Map<Instruction, Instruction> instructions = new IdentityHashMap<>();
      for (BasicBlock sourceBlock : source.getBlocks()) {
        BasicBlock cloneBlock = (BasicBlock) values.get(sourceBlock);
        for (Instruction sourceInstruction : sourceBlock.getInstructions()) {
          Instruction copy = sourceInstruction.copyWithoutOperands();
          cloneBlock.addInstruction(copy);
          values.put(sourceInstruction, copy);
          instructions.put(sourceInstruction, copy);
        }
      }
      for (Map.Entry<Instruction, Instruction> entry : instructions.entrySet()) {
        Instruction sourceInstruction = entry.getKey();
        Instruction copy = entry.getValue();
        for (int operand = 0; operand < sourceInstruction.getNumOperands(); operand++) {
          Value value = sourceInstruction.getOperand(operand);
          copy.addOperand(values.getOrDefault(value, value));
        }
      }
      module.addFunction(clone);
      return clone;
    }

    private static String freshName(Module module, String requested) {
      String name = requested;
      int suffix = 1;
      while (containsName(module, name)) {
        name = requested + "." + suffix++;
      }
      return name;
    }

    private static boolean containsName(Module module, String name) {
      for (Function function : module.getFunctions()) {
        if (name.equals(function.getName())) return true;
      }
      return false;
    }

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

    private static boolean hasReturn(Function function) {
      return function.getBlocks().stream().anyMatch(block ->
          block.getTerminator() != null
              && block.getTerminator().getOpcode() == Instruction.Opcode.RET);
    }

    private static int instructionCount(Module module) {
      return module.getFunctions().stream().mapToInt(Pass::instructionCount).sum();
    }

    private static int instructionCount(Function function) {
      return function.getBlocks().stream().mapToInt(block -> block.getInstructions().size()).sum();
    }

    private record SpecializationKey(Map<Integer, Constant> constants) {
      SpecializationKey {
        constants = Map.copyOf(constants);
      }
    }

    private record Specialization(SpecializationKey key, Function function) {}
  }
}
