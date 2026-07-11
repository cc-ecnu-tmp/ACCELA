package accela.pass.ir.transform.loopinvariantcse;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Hoists duplicated, always-executed loop-invariant expressions. */
public final class LoopInvariantCSE {
  private LoopInvariantCSE() {}

  static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (LoopAnalysis.Loop loop : fam.getResult(LoopAnalysis.class, function).loops()) {
      BasicBlock preheader = loop.preheader();
      if (preheader == null || preheader.getTerminator() == null) continue;
      Map<Expression, List<Instruction>> groups = new HashMap<>();
      for (BasicBlock block : loop.blocks()) {
        for (Instruction instruction : block.getInstructions()) {
          if (isCandidate(instruction, loop)) {
            groups.computeIfAbsent(expressionFor(instruction), ignored -> new ArrayList<>())
                .add(instruction);
          }
        }
      }
      for (List<Instruction> duplicates : groups.values()) {
        if (duplicates.size() < 2) continue;
        Instruction leader = duplicates.stream()
            .filter(instruction -> instruction.getParent() == loop.header())
            .findFirst().orElse(null);
        if (leader == null) continue;
        leader.getParent().remove(leader);
        preheader.insertInstructionBefore(preheader.getTerminator(), leader);
        for (Instruction duplicate : duplicates) {
          if (duplicate == leader) continue;
          duplicate.replaceAllUsesWith(leader);
          duplicate.eraseFromParent();
        }
        changed = true;
      }
    }
    return changed;
  }

  private static boolean isCandidate(
      Instruction instruction, LoopAnalysis.Loop loop) {
    if (!isSafe(instruction, loop)) return false;
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      Value operand = instruction.getOperand(index);
      if (operand instanceof Instruction definition
          && loop.blocks().contains(definition.getParent())) return false;
    }
    return true;
  }

  private static boolean isSafe(
      Instruction instruction, LoopAnalysis.Loop loop) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, XOR, AND, ZEXT, SEXT -> true;
      case SDIV, SREM -> instruction.getOperand(1) instanceof Constant.Int divisor
          && divisor.value != 0;
      case LOAD -> instruction.getOperand(0) instanceof GlobalVariable global
          && !global.getValueType().isArray()
          && !loopMayWriteGlobal(loop, global);
      default -> false;
    };
  }

  private static boolean loopMayWriteGlobal(
      LoopAnalysis.Loop loop, GlobalVariable global) {
    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.CALL) return true;
        if (instruction.getOpcode() == Instruction.Opcode.STORE
            && isDerivedFrom(instruction.getOperand(1), global)) return true;
      }
    }
    return false;
  }

  private static boolean isDerivedFrom(Value pointer, GlobalVariable global) {
    while (pointer instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.GEP) {
      pointer = instruction.getOperand(0);
    }
    return pointer == global;
  }

  private static Expression expressionFor(Instruction instruction) {
    List<Object> operands = new ArrayList<>();
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      Value operand = instruction.getOperand(index);
      operands.add(operand instanceof Constant.Int integer
          ? new IntegerKey(integer.getType().dataType, integer.value) : operand);
    }
    return new Expression(instruction.getOpcode(), instruction.getType(), List.copyOf(operands));
  }

  private record Expression(
      Instruction.Opcode opcode, Type type, List<Object> operands) {}
  private record IntegerKey(Type.DataType type, long value) {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopInvariantCSE.run(function, fam)
          ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
