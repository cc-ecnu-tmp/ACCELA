package accela.pass.ir.transform.lineartransition;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.IntegerLinearTransitionCandidate;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Legality matcher for small, simultaneous, constant-coefficient affine i32 transitions. */
final class IntegerLinearTransitionMatcher {
  static final int MAX_STATE_DIMENSION = 3;

  record MatchResult(Candidate candidate, String rejectedObligationId) {
    MatchResult {
      if ((candidate == null) == (rejectedObligationId == null)) {
        throw new IllegalArgumentException(
            "exactly one of candidate and rejectedObligationId is required");
      }
    }

    static MatchResult matched(Candidate candidate) {
      return new MatchResult(candidate, null);
    }

    static MatchResult rejected(String obligationId) {
      return new MatchResult(null, obligationId);
    }
  }

  record Candidate(
      LoopAnalysis.Loop loop,
      BasicBlock body,
      Instruction branch,
      int bodySuccessorOperand,
      Instruction induction,
      Value inductionStart,
      Value bound,
      List<Instruction> states,
      List<Value> stateStarts,
      int[][] homogeneousTransition,
      int transitionInstructionCount) {
    Candidate {
      states = List.copyOf(states);
      stateStarts = List.copyOf(stateStarts);
      homogeneousTransition = copyMatrix(homogeneousTransition);
      if (states.size() != stateStarts.size()) {
        throw new IllegalArgumentException("state and initial-value dimensions differ");
      }
      if (transitionInstructionCount < 0) {
        throw new IllegalArgumentException("transitionInstructionCount must not be negative");
      }
    }

    int stateDimension() {
      return states.size();
    }

    int matrixDimension() {
      return homogeneousTransition.length;
    }

    private static int[][] copyMatrix(int[][] source) {
      int[][] copy = new int[source.length][];
      for (int row = 0; row < source.length; row++) copy[row] = source[row].clone();
      return copy;
    }
  }

  private enum ExtractionFailure {
    AFFINE,
    MODULO,
    INTEGER
  }

  private static final class NotAffine extends Exception {
    final ExtractionFailure failure;

    NotAffine(ExtractionFailure failure) {
      this.failure = failure;
    }
  }

  private record AffineForm(int[] coefficients, int constant) {
    AffineForm {
      coefficients = coefficients.clone();
    }

    boolean dependsOnState() {
      for (int coefficient : coefficients) {
        if (coefficient != 0) return true;
      }
      return false;
    }
  }

  private IntegerLinearTransitionMatcher() {}

  static MatchResult match(
      LoopAnalysis.Loop loop,
      InductionVariableAnalysis.Result inductionVariables,
      ScalarEvolutionAnalysis.Result scalarEvolution) {
    if (loop.preheader() == null || loop.blocks().size() != 2 || loop.latches().size() != 1) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    BasicBlock header = loop.header();
    BasicBlock body = loop.latches().iterator().next();
    if (body == header) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    Instruction branch = header.getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    boolean trueInside = loop.contains((BasicBlock) branch.getOperand(1));
    boolean falseInside = loop.contains((BasicBlock) branch.getOperand(2));
    if (trueInside == falseInside) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    int bodySuccessorOperand = trueInside ? 1 : 2;
    if (branch.getOperand(bodySuccessorOperand) != body) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }

    List<Instruction> phis = header.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
    String predicate = trueInside ? compare.getPredicate() : invert(compare.getPredicate());
    if (predicate == null) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    Instruction induction = null;
    Value bound = null;
    if (compare.getOperand(0) instanceof Instruction phi && phis.contains(phi)) {
      induction = phi;
      bound = compare.getOperand(1);
    } else if (compare.getOperand(1) instanceof Instruction phi && phis.contains(phi)) {
      induction = phi;
      bound = compare.getOperand(0);
      predicate = swap(predicate);
    }
    if (induction == null
        || !"slt".equals(predicate)
        || induction.getType() != Type.INT
        || bound.getType() != Type.INT
        || !scalarEvolution.isLoopInvariant(scalarEvolution.getSCEV(bound), loop)) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }

    Instruction selectedInduction = induction;
    InductionVariableAnalysis.Induction inductionInfo = inductionVariables.inductions().stream()
        .filter(item -> item.loop() == loop && item.phi() == selectedInduction)
        .findFirst()
        .orElse(null);
    if (inductionInfo == null
        || inductionInfo.step() != 1
        || inductionInfo.latch() != body
        || inductionInfo.next().getParent() != body) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    if (!(inductionInfo.start() instanceof Constant.Int start)
        || (int) start.value < 0) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.ZERO_NEGATIVE_ITERATIONS);
    }

    List<Instruction> states = phis.stream().filter(phi -> phi != selectedInduction).toList();
    if (states.isEmpty() || states.size() > MAX_STATE_DIMENSION) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.SMALL_FIXED_DIMENSION);
    }
    if (states.stream().anyMatch(state -> state.getType() != Type.INT)) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.INTEGER_ONLY);
    }
    if (header.getInstructions().stream().anyMatch(instruction ->
        instruction.getOpcode() != Instruction.Opcode.PHI
            && instruction != compare
            && instruction != branch)) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.SIDE_EFFECT_FREE_BODY);
    }

    Instruction bodyBranch = body.getTerminator();
    if (bodyBranch == null
        || bodyBranch.getOpcode() != Instruction.Opcode.BR
        || bodyBranch.getOperand(0) != header) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
    }
    for (Instruction instruction : body.getInstructions()) {
      if (instruction == bodyBranch || instruction == inductionInfo.next()) continue;
      if (isEffectfulOrMemory(instruction)) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.SIDE_EFFECT_FREE_BODY);
      }
      if (instruction.getType() != Type.INT) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.INTEGER_ONLY);
      }
      if (instruction.getUses().stream()
          .anyMatch(use -> !loop.contains(use.getUser().getParent()))) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.LIVE_OUTS);
      }
    }

    List<Value> stateStarts = new ArrayList<>();
    List<Value> stateNextValues = new ArrayList<>();
    for (Instruction state : states) {
      if (state.getNumOperands() != 4) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.EXACT_TRIP_COUNT);
      }
      Value initial = incomingValue(state, loop.preheader());
      Value next = incomingValue(state, body);
      if (initial == null || next == null) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.AFFINE_TRANSITION);
      }
      if (initial.getType() != Type.INT) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.INTEGER_ONLY);
      }
      if (!scalarEvolution.isLoopInvariant(scalarEvolution.getSCEV(initial), loop)) {
        return MatchResult.rejected(IntegerLinearTransitionCandidate.AFFINE_TRANSITION);
      }
      stateStarts.add(initial);
      stateNextValues.add(next);
    }

    Map<Instruction, Integer> stateIndices = new IdentityHashMap<>();
    for (int index = 0; index < states.size(); index++) stateIndices.put(states.get(index), index);
    Set<Instruction> usedTransitionInstructions = new LinkedHashSet<>();
    int dimension = states.size();
    int[][] matrix = new int[dimension + 1][dimension + 1];
    Map<Value, AffineForm> cache = new IdentityHashMap<>();
    try {
      for (int row = 0; row < dimension; row++) {
        AffineForm form = extract(
            stateNextValues.get(row), body, stateIndices, dimension,
            usedTransitionInstructions, cache, new LinkedHashSet<>());
        System.arraycopy(form.coefficients(), 0, matrix[row], 0, dimension);
        matrix[row][dimension] = form.constant();
      }
    } catch (NotAffine failure) {
      String obligation = switch (failure.failure) {
        case AFFINE -> IntegerLinearTransitionCandidate.AFFINE_TRANSITION;
        case MODULO -> IntegerLinearTransitionCandidate.MODULO_I32_EQUIVALENCE;
        case INTEGER -> IntegerLinearTransitionCandidate.INTEGER_ONLY;
      };
      return MatchResult.rejected(obligation);
    }
    matrix[dimension][dimension] = 1;

    Set<Instruction> allowed = new LinkedHashSet<>(usedTransitionInstructions);
    allowed.add(inductionInfo.next());
    allowed.add(bodyBranch);
    if (body.getInstructions().stream().anyMatch(instruction -> !allowed.contains(instruction))) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.SIDE_EFFECT_FREE_BODY);
    }
    if (inductionInfo.next().getUses().stream()
        .anyMatch(use -> !loop.contains(use.getUser().getParent()))) {
      return MatchResult.rejected(IntegerLinearTransitionCandidate.LIVE_OUTS);
    }

    return MatchResult.matched(new Candidate(
        loop,
        body,
        branch,
        bodySuccessorOperand,
        selectedInduction,
        inductionInfo.start(),
        bound,
        states,
        stateStarts,
        matrix,
        usedTransitionInstructions.size()));
  }

  private static AffineForm extract(
      Value value,
      BasicBlock body,
      Map<Instruction, Integer> stateIndices,
      int dimension,
      Set<Instruction> usedInstructions,
      Map<Value, AffineForm> cache,
      Set<Value> active) throws NotAffine {
    Integer stateIndex = value instanceof Instruction instruction
        ? stateIndices.get(instruction) : null;
    if (stateIndex != null) {
      int[] coefficients = new int[dimension];
      coefficients[stateIndex] = 1;
      return new AffineForm(coefficients, 0);
    }
    if (value instanceof Constant.Int constant) {
      return new AffineForm(new int[dimension], (int) constant.value);
    }
    AffineForm cached = cache.get(value);
    if (cached != null) return cached;
    if (!(value instanceof Instruction instruction) || instruction.getParent() != body) {
      throw new NotAffine(ExtractionFailure.AFFINE);
    }
    if (instruction.getType() != Type.INT) throw new NotAffine(ExtractionFailure.INTEGER);
    if (!active.add(value)) throw new NotAffine(ExtractionFailure.AFFINE);
    AffineForm result;
    try {
      result = switch (instruction.getOpcode()) {
        case ADD -> add(
            extract(instruction.getOperand(0), body, stateIndices, dimension,
                usedInstructions, cache, active),
            extract(instruction.getOperand(1), body, stateIndices, dimension,
                usedInstructions, cache, active),
            1);
        case SUB -> add(
            extract(instruction.getOperand(0), body, stateIndices, dimension,
                usedInstructions, cache, active),
            extract(instruction.getOperand(1), body, stateIndices, dimension,
                usedInstructions, cache, active),
            -1);
        case MUL -> multiply(
            extract(instruction.getOperand(0), body, stateIndices, dimension,
                usedInstructions, cache, active),
            extract(instruction.getOperand(1), body, stateIndices, dimension,
                usedInstructions, cache, active));
        case SDIV, SREM, SMULH, SHL, ASHR, AND, XOR ->
            throw new NotAffine(ExtractionFailure.MODULO);
        default -> throw new NotAffine(ExtractionFailure.AFFINE);
      };
    } finally {
      active.remove(value);
    }
    usedInstructions.add(instruction);
    cache.put(value, result);
    return result;
  }

  private static AffineForm add(AffineForm left, AffineForm right, int rightSign) {
    int[] coefficients = left.coefficients().clone();
    for (int index = 0; index < coefficients.length; index++) {
      coefficients[index] += rightSign * right.coefficients()[index];
    }
    return new AffineForm(coefficients, left.constant() + rightSign * right.constant());
  }

  private static AffineForm multiply(AffineForm left, AffineForm right) throws NotAffine {
    if (left.dependsOnState() && right.dependsOnState()) {
      throw new NotAffine(ExtractionFailure.AFFINE);
    }
    if (!left.dependsOnState()) return scale(right, left.constant());
    return scale(left, right.constant());
  }

  private static AffineForm scale(AffineForm form, int factor) {
    int[] coefficients = form.coefficients().clone();
    for (int index = 0; index < coefficients.length; index++) coefficients[index] *= factor;
    return new AffineForm(coefficients, form.constant() * factor);
  }

  private static boolean isEffectfulOrMemory(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ALLOCA, LOAD, STORE, GEP, CALL,
          FADD, FSUB, FMUL, FDIV, FNEG, FCMP, SITOFP, FPTOSI -> true;
      default -> false;
    };
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index + 1 < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static String invert(String predicate) {
    return switch (predicate) {
      case "slt" -> "sge";
      case "sle" -> "sgt";
      case "sgt" -> "sle";
      case "sge" -> "slt";
      case "eq" -> "ne";
      case "ne" -> "eq";
      default -> null;
    };
  }

  private static String swap(String predicate) {
    return switch (predicate) {
      case "slt" -> "sgt";
      case "sle" -> "sge";
      case "sgt" -> "slt";
      case "sge" -> "sle";
      case "eq", "ne" -> predicate;
      default -> null;
    };
  }
}
