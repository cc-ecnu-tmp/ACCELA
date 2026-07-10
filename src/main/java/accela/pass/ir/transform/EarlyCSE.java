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

/** Eliminates repeated side-effect-free expressions within a basic block. */
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
      Map<Expression, Value> available = new HashMap<>();
      Map<Value, Value> availableLoads = new IdentityHashMap<>();
      for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
        if (instruction.getOpcode() == Instruction.Opcode.STORE) {
          availableLoads.clear();
          availableLoads.put(instruction.getOperand(1), instruction.getOperand(0));
          continue;
        }
        if (instruction.getOpcode() == Instruction.Opcode.CALL) {
          availableLoads.clear();
          continue;
        }
        if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
          Value pointer = instruction.getOperand(0);
          Value existing = availableLoads.get(pointer);
          if (existing == null) {
            availableLoads.put(pointer, instruction);
            continue;
          }
          instruction.replaceAllUsesWith(existing);
          instruction.eraseFromParent();
          changed = true;
          continue;
        }
        if (!isSimple(instruction)) continue;
        Expression expression = expressionFor(instruction);
        Value existing = available.get(expression);
        if (existing == null) {
          available.put(expression, instruction);
          continue;
        }
        instruction.replaceAllUsesWith(existing);
        instruction.eraseFromParent();
        changed = true;
      }
    }
    return changed;
  }

  private static boolean isSimple(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SDIV, SREM, FADD, FSUB, FMUL, FDIV, FNEG,
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
