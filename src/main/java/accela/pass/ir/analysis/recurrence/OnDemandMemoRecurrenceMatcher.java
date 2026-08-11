package accela.pass.ir.analysis.recurrence;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Objects;
import java.util.Set;

/**
 * Recognizes the RRT2 subset: pure direct recursion over a two-component well-founded rank.
 *
 * <p>This deliberately does not accept the ordinary one-argument Fibonacci shape already covered
 * by ranked recurrence tabulation. In the memoized nonnegative domain, every recursive edge is
 * component-wise non-increasing and strictly decreases at least one component. Consequently the
 * sum of both components is a finite rank without relying on signed-overflow assumptions.
 */
public final class OnDemandMemoRecurrenceMatcher {
  public static final String CANDIDATE_ID = "candidate.rrt2-on-demand-memoization";
  public static final String PREFIX = CANDIDATE_ID + ".";
  public static final String DIRECT_FINITE_RECURSION = PREFIX + "direct-finite-recursion";
  public static final String COMPOSITE_WELL_FOUNDED_RANK =
      PREFIX + "composite-well-founded-rank";
  public static final String PURE_CONTEXT = PREFIX + "pure-context";
  public static final String REACHABLE_DOMAIN = PREFIX + "reachable-domain";
  public static final String MEMO_KEY_INJECTIVE = PREFIX + "memo-key-injective";
  public static final String BASE_CASE_ORDER = PREFIX + "base-case-order";
  public static final String MODULO_I32_EQUIVALENCE = PREFIX + "modulo-i32-equivalence";
  public static final String BOUNDED_STORAGE = PREFIX + "bounded-storage";
  public static final String NO_ABI_RUNTIME_CHANGE = PREFIX + "no-abi-runtime-change";
  public static final String PROFITABILITY = PREFIX + "profitability";

  private OnDemandMemoRecurrenceMatcher() {}

  /** Result of inspecting one function. Non-recursive functions are not candidate decisions. */
  public record Result(
      boolean considered,
      OnDemandMemoRecurrence recurrence,
      String rejectedObligationId) {
    public Result {
      if (!considered) {
        if (recurrence != null || rejectedObligationId != null) {
          throw new IllegalArgumentException("an unconsidered function has no RRT2 decision");
        }
      } else if ((recurrence == null) == (rejectedObligationId == null)) {
        throw new IllegalArgumentException(
            "a considered function must be exactly one of matched or rejected");
      }
    }

    public boolean matched() {
      return recurrence != null;
    }
  }

  public static Result inspect(Function function) {
    FunctionAnalysisManager analyses = new FunctionAnalysisManager();
    analyses.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    return inspect(
        function, analyses.getResult(DominatorTreeAnalysis.class, function));
  }

  /** Inspects a function while reusing the caller's cached production dominator analysis. */
  public static Result inspect(
      Function function, DominatorTreeAnalysis.Result dominators) {
    Objects.requireNonNull(function, "function");
    Objects.requireNonNull(dominators, "dominators");
    List<Instruction> directCalls = directSelfCalls(function);
    if (directCalls.isEmpty()) return new Result(false, null, null);

    if (function.getReturnType() != Type.INT
        || function.getNumArgs() != 2
        || function.getArguments().stream().anyMatch(argument -> argument.getType() != Type.INT)) {
      return rejected(MODULO_I32_EQUIVALENCE);
    }
    if (function.getBlocks().isEmpty() || hasCfgCycle(function)) {
      return rejected(DIRECT_FINITE_RECURSION);
    }

    List<Instruction> recursiveCalls = new ArrayList<>();
    for (BasicBlock block : function.getBlocks()) {
      if (block.getTerminator() == null) return rejected(DIRECT_FINITE_RECURSION);
      for (Instruction instruction : block.getInstructions()) {
        String rejection = validateInstruction(instruction, function, recursiveCalls);
        if (rejection != null) return rejected(rejection);
      }
    }
    if (recursiveCalls.isEmpty()) return new Result(false, null, null);

    List<OnDemandMemoRecurrence.Transition> transitions = new ArrayList<>();
    boolean firstParticipates = false;
    boolean secondParticipates = false;
    for (Instruction call : recursiveCalls) {
      int first = decrease(call.getOperand(0), function.getArguments().get(0));
      int second = decrease(call.getOperand(1), function.getArguments().get(1));
      if (first < 0 || second < 0 || first == 0 && second == 0) {
        return rejected(COMPOSITE_WELL_FOUNDED_RANK);
      }
      firstParticipates |= first > 0;
      secondParticipates |= second > 0;
      transitions.add(new OnDemandMemoRecurrence.Transition(first, second));
    }
    // Both components must be meaningful rank dimensions. This is the explicit RRT2/ordinary-RRT
    // boundary and rejects a dummy invariant parameter around one-dimensional Fibonacci.
    if (!firstParticipates || !secondParticipates) {
      return rejected(COMPOSITE_WELL_FOUNDED_RANK);
    }
    OnDemandMemoRecurrence.DomainShape domainShape =
        reachableDomain(function, recursiveCalls, transitions, dominators);
    if (domainShape == null) return rejected(REACHABLE_DOMAIN);
    return new Result(
        true,
        new OnDemandMemoRecurrence(function, recursiveCalls, transitions, domainShape),
        null);
  }

  public static List<String> obligationIds() {
    return List.of(
        DIRECT_FINITE_RECURSION,
        COMPOSITE_WELL_FOUNDED_RANK,
        PURE_CONTEXT,
        REACHABLE_DOMAIN,
        MEMO_KEY_INJECTIVE,
        BASE_CASE_ORDER,
        MODULO_I32_EQUIVALENCE,
        BOUNDED_STORAGE,
        NO_ABI_RUNTIME_CHANGE,
        PROFITABILITY);
  }

  private static Result rejected(String obligationId) {
    return new Result(true, null, obligationId);
  }

  private static List<Instruction> directSelfCalls(Function function) {
    return function.getBlocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL)
        .filter(instruction -> instruction.getCallee() == function)
        .toList();
  }

  private static String validateInstruction(
      Instruction instruction, Function function, List<Instruction> recursiveCalls) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL -> binaryI32(instruction) ? null : MODULO_I32_EQUIVALENCE;
      case ICMP -> instruction.getType() == Type.I1
              && instruction.getNumOperands() == 2
              && instruction.getOperand(0).getType() == Type.INT
              && instruction.getOperand(1).getType() == Type.INT
          ? null : MODULO_I32_EQUIVALENCE;
      case XOR, AND -> sameBooleanOrI32Operands(instruction)
          ? null : MODULO_I32_EQUIVALENCE;
      case ZEXT, SEXT -> instruction.getNumOperands() == 1
              && instruction.getType() == Type.INT
              && instruction.getOperand(0).getType() == Type.I1
          ? null : MODULO_I32_EQUIVALENCE;
      case BR -> instruction.getNumOperands() == 1 ? null : DIRECT_FINITE_RECURSION;
      case CONDBR -> instruction.getNumOperands() == 3
              && instruction.getOperand(0).getType() == Type.I1
          ? null : DIRECT_FINITE_RECURSION;
      case PHI -> phiIsScalar(instruction) ? null : MODULO_I32_EQUIVALENCE;
      case RET -> instruction.getNumOperands() == 1
              && instruction.getOperand(0).getType() == Type.INT
          ? null : DIRECT_FINITE_RECURSION;
      case CALL -> {
        if (instruction.getCallee() != function
            || instruction.getNumOperands() != 2
            || instruction.getType() != Type.INT
            || instruction.getOperand(0).getType() != Type.INT
            || instruction.getOperand(1).getType() != Type.INT) {
          yield PURE_CONTEXT;
        }
        recursiveCalls.add(instruction);
        yield null;
      }
      // Memory, external calls, floating point, division, shifts, and allocation are outside the
      // deliberately pure and easily auditable RRT2 subset.
      default -> PURE_CONTEXT;
    };
  }

  private static boolean binaryI32(Instruction instruction) {
    return instruction.getType() == Type.INT
        && instruction.getNumOperands() == 2
        && instruction.getOperand(0).getType() == Type.INT
        && instruction.getOperand(1).getType() == Type.INT;
  }

  private static boolean sameBooleanOrI32Operands(Instruction instruction) {
    if (instruction.getNumOperands() != 2) return false;
    Type type = instruction.getType();
    return (type == Type.I1 || type == Type.INT)
        && instruction.getOperand(0).getType() == type
        && instruction.getOperand(1).getType() == type;
  }

  private static boolean phiIsScalar(Instruction instruction) {
    if (instruction.getType() != Type.INT && instruction.getType() != Type.I1) return false;
    if (instruction.getNumOperands() == 0 || (instruction.getNumOperands() & 1) != 0) return false;
    for (int index = 0; index < instruction.getNumOperands(); index += 2) {
      if (instruction.getOperand(index).getType() != instruction.getType()
          || !(instruction.getOperand(index + 1) instanceof BasicBlock)) return false;
    }
    return true;
  }

  /** Returns zero for identity, a positive decrease, or -1 for an unsupported expression. */
  private static int decrease(Value value, Function.Argument argument) {
    if (value == argument) return 0;
    if (!(value instanceof Instruction instruction)
        || instruction.getNumOperands() != 2
        || instruction.getOperand(0) != argument
        || !(instruction.getOperand(1) instanceof Constant.Int amount)) return -1;
    long decrease = switch (instruction.getOpcode()) {
      case SUB -> amount.value;
      case ADD -> -amount.value;
      default -> -1;
    };
    return decrease > 0 && decrease <= Integer.MAX_VALUE ? (int) decrease : -1;
  }

  private static OnDemandMemoRecurrence.DomainShape reachableDomain(
      Function function,
      List<Instruction> calls,
      List<OnDemandMemoRecurrence.Transition> transitions,
      DominatorTreeAnalysis.Result dominators) {
    boolean rectangular = true;
    for (int index = 0; index < calls.size(); index++) {
      Instruction call = calls.get(index);
      OnDemandMemoRecurrence.Transition transition = transitions.get(index);
      if (transition.firstDecrease() > 1 || transition.secondDecrease() > 1
          || transition.firstDecrease() == 1
              && !knownNonZero(function.getArguments().get(0), call, dominators)
          || transition.secondDecrease() == 1
              && !knownNonZero(function.getArguments().get(1), call, dominators)) {
        rectangular = false;
        break;
      }
    }
    if (rectangular) {
      return OnDemandMemoRecurrence.DomainShape.RECTANGULAR_NONNEGATIVE;
    }

    // Pascal/binomial domain: 0 <= second <= first. Its two legal edges are closed because
    // second!=0 protects (first-1, second-1), while second!=first protects (first-1, second).
    boolean triangular = true;
    for (int index = 0; index < calls.size(); index++) {
      Instruction call = calls.get(index);
      OnDemandMemoRecurrence.Transition transition = transitions.get(index);
      boolean protectedEdge = transition.firstDecrease() == 1
          && (transition.secondDecrease() == 1
              && knownNonZero(function.getArguments().get(1), call, dominators)
              || transition.secondDecrease() == 0
              && knownNotEqual(
                  function.getArguments().get(0),
                  function.getArguments().get(1),
                  call,
                  dominators));
      if (!protectedEdge) {
        triangular = false;
        break;
      }
    }
    return triangular
        ? OnDemandMemoRecurrence.DomainShape.TRIANGULAR_NONNEGATIVE
        : null;
  }

  private static boolean knownNonZero(
      Function.Argument argument,
      Instruction use,
      DominatorTreeAnalysis.Result dominators) {
    for (BasicBlock block : argument.getParent().getBlocks()) {
      Boolean outcome = dominatingBranchOutcome(block, use.getParent(), dominators);
      if (outcome == null) continue;
      Instruction compare = comparison(block);
      if (compare == null) continue;
      Value left = compare.getOperand(0);
      Value right = compare.getOperand(1);
      if (right == argument && isZero(left)) {
        Value temporary = left;
        left = right;
        right = temporary;
      }
      if (left != argument || !isZero(right)) continue;
      if (compare.getPredicate().equals("eq") && !outcome
          || compare.getPredicate().equals("ne") && outcome) return true;
    }
    return false;
  }

  private static boolean knownNotEqual(
      Function.Argument first,
      Function.Argument second,
      Instruction use,
      DominatorTreeAnalysis.Result dominators) {
    for (BasicBlock block : first.getParent().getBlocks()) {
      Boolean outcome = dominatingBranchOutcome(block, use.getParent(), dominators);
      if (outcome == null) continue;
      Instruction compare = comparison(block);
      if (compare == null) continue;
      boolean samePair = compare.getOperand(0) == first && compare.getOperand(1) == second
          || compare.getOperand(0) == second && compare.getOperand(1) == first;
      if (!samePair) continue;
      if (compare.getPredicate().equals("eq") && !outcome
          || compare.getPredicate().equals("ne") && outcome) return true;
    }
    return false;
  }

  private static Boolean dominatingBranchOutcome(
      BasicBlock branchBlock,
      BasicBlock useBlock,
      DominatorTreeAnalysis.Result dominators) {
    Instruction branch = branchBlock.getTerminator();
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR) return null;
    BasicBlock trueSuccessor = (BasicBlock) branch.getOperand(1);
    BasicBlock falseSuccessor = (BasicBlock) branch.getOperand(2);
    if (dominators.dominates(trueSuccessor, useBlock)) return true;
    if (dominators.dominates(falseSuccessor, useBlock)) return false;
    return null;
  }

  private static Instruction comparison(BasicBlock block) {
    Instruction branch = block.getTerminator();
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction comparison)
        || comparison.getOpcode() != Instruction.Opcode.ICMP) return null;
    return comparison;
  }

  private static boolean isZero(Value value) {
    return value instanceof Constant.Int integer && integer.value == 0;
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
