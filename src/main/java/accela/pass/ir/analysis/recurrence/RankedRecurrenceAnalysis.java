package accela.pass.ir.analysis.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Function.Argument;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;

/** Recognizes a conservative, dense-safe subset of direct finite ranked recursion. */
public final class RankedRecurrenceAnalysis {
  private RankedRecurrenceAnalysis() {}

  public static RankedRecurrence analyze(
      Function function, DominatorTreeAnalysis.Result dominators) {
    if (function.getReturnType() != Type.INT
        || function.getNumArgs() == 0
        || hasCfgCycle(function)) return null;

    List<Instruction> calls = new ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (!isDenseSafe(instruction, function, calls)) return null;
      }
    }
    if (calls.size() < 2) return null;

    int rank = rankArgument(function, calls);
    if (rank < 0) return null;
    List<Integer> states = changingArguments(function, calls, rank);
    if (states == null || !contextIsInvariant(function, calls, rank, states)) return null;

    int rankLimit =
        rankLimit(function, function.getArguments().get(rank), dominators);
    return rankLimit == 0
        ? null
        : new RankedRecurrence(function, rank, states, rankLimit, calls);
  }

  private static boolean isDenseSafe(
      Instruction instruction, Function function, List<Instruction> calls) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, ICMP, BR, CONDBR, ZEXT, SEXT, XOR, AND, PHI -> true;
      case RET -> instruction.getNumOperands() == 1;
      case LOAD -> true;
      case GEP -> instruction.getUses().stream()
          .allMatch(use -> use.getOperandIndex() == 0
              && use.getUser().getOpcode() == Instruction.Opcode.LOAD);
      case CALL -> {
        if (instruction.getCallee() != function
            || instruction.getNumOperands() != function.getNumArgs()) yield false;
        calls.add(instruction);
        yield true;
      }
      default -> false;
    };
  }

  private static int rankArgument(Function function, List<Instruction> calls) {
    for (int index = 0; index < function.getNumArgs(); index++) {
      int argumentIndex = index;
      Argument argument = function.getArguments().get(index);
      if (argument.getType() != Type.INT
          || !calls.stream()
              .allMatch(call -> isStrictDecrement(call.getOperand(argumentIndex), argument))) {
        continue;
      }
      return index;
    }
    return -1;
  }

  private static List<Integer> changingArguments(
      Function function, List<Instruction> calls, int rank) {
    List<Integer> states = new ArrayList<>();
    for (int index = 0; index < function.getNumArgs(); index++) {
      if (index == rank) continue;
      int argumentIndex = index;
      Argument argument = function.getArguments().get(index);
      boolean invariant =
          calls.stream().allMatch(call -> call.getOperand(argumentIndex) == argument);
      if (invariant) continue;
      if (argument.getType() != Type.INT) return null;
      states.add(index);
    }
    return List.copyOf(states);
  }

  private static boolean contextIsInvariant(
      Function function, List<Instruction> calls, int rank, List<Integer> states) {
    for (int index = 0; index < function.getNumArgs(); index++) {
      if (index == rank || states.contains(index)) continue;
      int argumentIndex = index;
      Value argument = function.getArguments().get(index);
      if (!calls.stream().allMatch(call -> call.getOperand(argumentIndex) == argument)) return false;
    }
    return true;
  }

  private static int rankLimit(
      Function function, Argument rank, DominatorTreeAnalysis.Result dominators) {
    int limit = Integer.MAX_VALUE;
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
          if (instruction.getType() != Type.INT
              || !isStableLoadPointer(instruction.getOperand(0))) return 0;
          continue;
        }
        if (instruction.getOpcode() != Instruction.Opcode.GEP) continue;
        Type source = instruction.getGepSourceType();
        if (!(instruction.getOperand(0) instanceof GlobalVariable)
            || source == null
            || !source.isArray()
            || source.innerType != Type.INT
            || instruction.getNumOperands() != 3
            || !isZero(instruction.getOperand(1))
            || !isUnitDecrement(stripSExt(instruction.getOperand(2)), rank)
            || !isGuardedByPositiveRank(instruction, rank, function, dominators)) return 0;
        limit = Math.min(limit, source.size);
      }
    }
    return limit;
  }

  private static boolean isStableLoadPointer(Value pointer) {
    return pointer instanceof GlobalVariable
        || pointer instanceof Instruction address
            && address.getOpcode() == Instruction.Opcode.GEP;
  }

  /**
   * Dense tabulation evaluates states absent from the original call tree. Require every rank-1
   * array access to remain behind a branch proving rank >= 1 in the generated nonnegative domain.
   */
  private static boolean isGuardedByPositiveRank(
      Instruction access,
      Argument rank,
      Function function,
      DominatorTreeAnalysis.Result dominators) {
    for (BasicBlock block : function.getBlocks()) {
      Instruction terminator = block.getTerminator();
      BasicBlock positive = positiveRankSuccessor(terminator, rank);
      if (positive != null && dominators.dominates(positive, access.getParent())) return true;
    }
    return false;
  }

  private static BasicBlock positiveRankSuccessor(Instruction branch, Argument rank) {
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) return null;
    Value left = compare.getOperand(0);
    Value right = compare.getOperand(1);
    String predicate = compare.getPredicate();
    if (right == rank && left instanceof Constant.Int) {
      Value temporary = left;
      left = right;
      right = temporary;
      predicate = swapPredicate(predicate);
    }
    if (left != rank || !(right instanceof Constant.Int bound)) return null;
    boolean trueIsPositive = switch (predicate) {
      case "ne" -> bound.value == 0;
      case "sgt" -> bound.value >= 0;
      case "sge" -> bound.value >= 1;
      default -> false;
    };
    boolean falseIsPositive = switch (predicate) {
      case "eq" -> bound.value == 0;
      case "slt" -> bound.value >= 1;
      case "sle" -> bound.value >= 0;
      default -> false;
    };
    if (trueIsPositive == falseIsPositive) return null;
    return (BasicBlock) branch.getOperand(trueIsPositive ? 1 : 2);
  }

  private static String swapPredicate(String predicate) {
    return switch (predicate) {
      case "slt" -> "sgt";
      case "sle" -> "sge";
      case "sgt" -> "slt";
      case "sge" -> "sle";
      default -> predicate;
    };
  }

  private static boolean isUnitDecrement(Value value, Argument argument) {
    return value instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.SUB
        && instruction.getOperand(0) == argument
        && isOne(instruction.getOperand(1));
  }

  private static boolean isStrictDecrement(Value value, Argument argument) {
    if (!(value instanceof Instruction instruction)
        || instruction.getOperand(0) != argument
        || !(instruction.getOperand(1) instanceof Constant.Int amount)) return false;
    return (instruction.getOpcode() == Instruction.Opcode.SUB && amount.value > 0)
        || (instruction.getOpcode() == Instruction.Opcode.ADD && amount.value < 0);
  }

  private static Value stripSExt(Value value) {
    return value instanceof Instruction instruction
            && instruction.getOpcode() == Instruction.Opcode.SEXT
        ? instruction.getOperand(0)
        : value;
  }

  private static boolean isZero(Value value) {
    return value instanceof Constant.Int integer && integer.value == 0;
  }

  private static boolean isOne(Value value) {
    return value instanceof Constant.Int integer && integer.value == 1;
  }

  private static boolean hasCfgCycle(Function function) {
    Set<BasicBlock> active = Collections.newSetFromMap(new IdentityHashMap<>());
    Set<BasicBlock> done = Collections.newSetFromMap(new IdentityHashMap<>());
    for (BasicBlock block : function.getBlocks()) {
      if (hasCycle(block, active, done)) return true;
    }
    return false;
  }

  private static boolean hasCycle(
      BasicBlock block, Set<BasicBlock> active, Set<BasicBlock> done) {
    if (done.contains(block)) return false;
    if (!active.add(block)) return true;
    for (BasicBlock successor : block.getSuccessors()) {
      if (hasCycle(successor, active, done)) return true;
    }
    active.remove(block);
    done.add(block);
    return false;
  }
}
