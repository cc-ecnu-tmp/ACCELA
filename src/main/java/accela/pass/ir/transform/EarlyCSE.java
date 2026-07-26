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
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  public static boolean runOnFunction(Function function) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      changed |= runOnBlock(block);
    }
    return changed;
  }

  private static boolean runOnBlock(BasicBlock block) {
    Map<Expression, Value> available = new HashMap<>();
    Map<Value, Value> availableLoads = new IdentityHashMap<>();
    boolean changed = false;
    for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
      Value replacement = switch (instruction.getOpcode()) {
        case STORE -> {
          availableLoads.clear();
          availableLoads.put(instruction.getOperand(1), instruction.getOperand(0));
          yield null;
        }
        case CALL -> {
          availableLoads.clear();
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
      case ADD, SUB, MUL, SMULH, SDIV, SREM, ASHR, FADD, FSUB, FMUL, FDIV, FNEG,
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
