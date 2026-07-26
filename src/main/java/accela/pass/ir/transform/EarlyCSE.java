package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/**

   Eliminates repeated expressions within a basic block.

   This is a simple version of GVN, but benefits are real.

   @todo: Add actual GVN.

 */
public final class EarlyCSE {
  private EarlyCSE() {}

  public static final class Pass implements FunctionPass {
    private accela.ir.Module cachedModule;
    private GlobalModRefAnalysis.Result modRef;

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (function.getModule() != cachedModule) {
        cachedModule = function.getModule();
        modRef = cachedModule == null ? null : GlobalModRefAnalysis.analyze(cachedModule);
      }
      return runOnFunction(function, modRef)
          ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  public static boolean runOnFunction(Function function) {
    accela.ir.Module module = function.getModule();
    return runOnFunction(
        function, module == null ? null : GlobalModRefAnalysis.analyze(module));
  }

  private static boolean runOnFunction(
      Function function, GlobalModRefAnalysis.Result modRef) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      changed |= runOnBlock(block, modRef);
    }
    return changed;
  }

  private static boolean runOnBlock(
      BasicBlock block, GlobalModRefAnalysis.Result modRef) {
    Map<Expression, Value> available = new HashMap<>();
    Map<Value, Value> availableLoads = new IdentityHashMap<>();
    boolean changed = false;
    for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
      Value replacement = switch (instruction.getOpcode()) {
        case STORE -> {
          Value pointer = instruction.getOperand(1);
          availableLoads.keySet().removeIf(
              loadedPointer -> PointerProvenance.mayAlias(loadedPointer, pointer));
          availableLoads.put(instruction.getOperand(1), instruction.getOperand(0));
          yield null;
        }
        case CALL -> {
          if (modRef == null) availableLoads.clear();
          else availableLoads.keySet().removeIf(
              pointer -> modRef.mayWrite(instruction, pointer));
          yield null;
        }
        case LOAD -> availableLoads.putIfAbsent(instruction.getOperand(0), instruction);
        default -> {
          if (!isSimple(instruction)) yield null;
          yield available.putIfAbsent(expressionFor(instruction), instruction);
        }
      };
      if (replacement == null) continue;
      instruction.replaceAllUsesWith(replacement);
      instruction.eraseFromParent();
      changed = true;
    }
    return changed;
  }

  private static boolean isSimple(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, FADD, FSUB, FMUL, FDIV, FNEG,
          ICMP, FCMP, GEP, ZEXT, SEXT, SITOFP, FPTOSI, XOR -> true;
      default -> false;
    };
  }

  private static Expression expressionFor(Instruction instruction) {
    List<Object> operands = new ArrayList<>();
    for (int i = 0; i < instruction.getNumOperands(); i++) {
      operands.add(valueKey(instruction.getOperand(i)));
    }
    String detail = switch (instruction.getOpcode()) {
      case ICMP, FCMP -> instruction.getPredicate();
      case GEP -> instruction.getGepSourceType() + ":" + instruction.isGepInbounds();
      default -> "";
    };
    return new Expression(
        instruction.getOpcode(), instruction.getType().toString(), detail, List.copyOf(operands));
  }

  private static Object valueKey(Value value) {
    if (value instanceof Constant.Int integer) {
      return new IntegerKey(integer.getType().dataType, integer.value);
    }
    if (value instanceof Constant.Float floating) {
      return new FloatKey(Float.floatToRawIntBits(floating.value));
    }
    return value;
  }

  private record Expression(
      Instruction.Opcode opcode, String type, String detail, List<Object> operands) {}

  private record IntegerKey(Type.DataType type, long value) {}

  private record FloatKey(int bits) {}
}
