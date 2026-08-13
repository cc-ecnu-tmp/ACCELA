package accela.pass.ir.transform.finitestate;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.ExactI32;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.FiniteStateAccelerationCandidate;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Legality matcher for one-scalar, closed, deterministic finite-state loops. */
final class FiniteStateAccelerationMatcher {
  static final int MAX_EVALUATED_DOMAIN = 4096;

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
      BasicBlock bodyEntry,
      Instruction induction,
      Value inductionStart,
      Value bound,
      Instruction state,
      Value stateStart,
      Value nextState,
      int domainSize,
      int[] transition,
      int transitionInstructionCount) {
    Candidate {
      transition = transition.clone();
      if (domainSize < 2 || transition.length != domainSize) {
        throw new IllegalArgumentException("transition table must cover the finite domain");
      }
      if (transitionInstructionCount < 1) {
        throw new IllegalArgumentException("transition must contain executable work");
      }
    }
  }

  private FiniteStateAccelerationMatcher() {}

  static MatchResult match(
      LoopAnalysis.Loop loop,
      InductionVariableAnalysis.Result inductionVariables,
      ScalarEvolutionAnalysis.Result scalarEvolution) {
    IterationShape iteration = matchIteration(loop, inductionVariables, scalarEvolution);
    if (iteration == null) {
      return MatchResult.rejected(FiniteStateAccelerationCandidate.ITERATION_DOMAIN);
    }

    StateShape state = matchState(loop, iteration, scalarEvolution);
    if (state == null) {
      return MatchResult.rejected(FiniteStateAccelerationCandidate.EXACT_STATE_ENCODING);
    }

    Integer domain = terminalDomain(state.nextState());
    if (domain == null || domain < 2 || domain > MAX_EVALUATED_DOMAIN) {
      return MatchResult.rejected(FiniteStateAccelerationCandidate.CONSTANT_FINITE_DOMAIN);
    }

    TransitionShape transition = validateTransitionCfg(loop, iteration, state);
    if (transition == null) {
      return MatchResult.rejected(
          FiniteStateAccelerationCandidate.DETERMINISTIC_PURE_TRANSITION);
    }

    int[] mapping = new int[domain];
    FiniteStateTransitionEvaluator evaluator = new FiniteStateTransitionEvaluator(
        loop,
        iteration.bodyEntry(),
        iteration.induction(),
        state.state(),
        state.nextState());
    for (int input = 0; input < domain; input++) {
      int output;
      try {
        output = evaluator.evaluate(input);
      } catch (FiniteStateTransitionEvaluator.EvaluationFailure failure) {
        return MatchResult.rejected(FiniteStateAccelerationCandidate.MODULO_I32_EQUIVALENCE);
      }
      if (output < 0 || output >= domain) {
        return MatchResult.rejected(FiniteStateAccelerationCandidate.TRANSITION_CLOSURE);
      }
      mapping[input] = output;
    }

    return MatchResult.matched(new Candidate(
        loop,
        iteration.bodyEntry(),
        iteration.induction(),
        iteration.inductionStart(),
        iteration.bound(),
        state.state(),
        state.stateStart(),
        state.nextState(),
        domain,
        mapping,
        transition.instructionCount()));
  }

  private record IterationShape(
      BasicBlock bodyEntry,
      Instruction headerBranch,
      int bodySuccessorOperand,
      Instruction induction,
      Value inductionStart,
      Value bound,
      Instruction inductionNext,
      BasicBlock latch) {}

  private static IterationShape matchIteration(
      LoopAnalysis.Loop loop,
      InductionVariableAnalysis.Result inductionVariables,
      ScalarEvolutionAnalysis.Result scalarEvolution) {
    if (loop.preheader() == null || loop.latches().size() != 1) return null;
    BasicBlock header = loop.header();
    Instruction preheaderBranch = loop.preheader().getTerminator();
    if (preheaderBranch == null
        || preheaderBranch.getOpcode() != Instruction.Opcode.BR
        || preheaderBranch.getOperand(0) != header) return null;
    Instruction branch = header.getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) return null;
    boolean trueInside = loop.contains((BasicBlock) branch.getOperand(1));
    boolean falseInside = loop.contains((BasicBlock) branch.getOperand(2));
    if (trueInside == falseInside) return null;
    int bodySuccessorOperand = trueInside ? 1 : 2;
    BasicBlock bodyEntry = (BasicBlock) branch.getOperand(bodySuccessorOperand);
    if (!bodyEntry.getInstructions().isEmpty()
        && bodyEntry.getInstructions().getFirst().getOpcode() == Instruction.Opcode.PHI) return null;

    String predicate = trueInside ? compare.getPredicate() : invert(compare.getPredicate());
    if (predicate == null) return null;
    Instruction induction = null;
    Value bound = null;
    if (compare.getOperand(0) instanceof Instruction phi
        && phi.getParent() == header
        && phi.getOpcode() == Instruction.Opcode.PHI) {
      induction = phi;
      bound = compare.getOperand(1);
    } else if (compare.getOperand(1) instanceof Instruction phi
        && phi.getParent() == header
        && phi.getOpcode() == Instruction.Opcode.PHI) {
      induction = phi;
      bound = compare.getOperand(0);
      predicate = swap(predicate);
    }
    if (induction == null
        || !"slt".equals(predicate)
        || induction.getType() != Type.INT
        || bound.getType() != Type.INT
        || !scalarEvolution.isLoopInvariant(scalarEvolution.getSCEV(bound), loop)) return null;

    Instruction selected = induction;
    InductionVariableAnalysis.Induction info = inductionVariables.inductions().stream()
        .filter(item -> item.loop() == loop && item.phi() == selected)
        .findFirst()
        .orElse(null);
    BasicBlock latch = loop.latches().iterator().next();
    if (info == null
        || info.step() != 1
        || info.latch() != latch
        || info.next().getParent() != latch
        || !(info.start() instanceof Constant.Int start)
        || ExactI32.normalize(start.value) < 0) return null;
    return new IterationShape(
        bodyEntry,
        branch,
        bodySuccessorOperand,
        selected,
        info.start(),
        bound,
        info.next(),
        latch);
  }

  private record StateShape(Instruction state, Value stateStart, Value nextState) {}

  private static StateShape matchState(
      LoopAnalysis.Loop loop,
      IterationShape iteration,
      ScalarEvolutionAnalysis.Result scalarEvolution) {
    List<Instruction> headerPhis = loop.header().getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
    List<Instruction> states = headerPhis.stream()
        .filter(phi -> phi != iteration.induction())
        .toList();
    if (states.size() != 1) return null;
    Instruction state = states.getFirst();
    if (state.getType() != Type.INT || state.getNumOperands() != 4) return null;
    Value start = incomingValue(state, loop.preheader());
    Value next = incomingValue(state, iteration.latch());
    if (start == null
        || next == null
        || start.getType() != Type.INT
        || next.getType() != Type.INT
        || !scalarEvolution.isLoopInvariant(scalarEvolution.getSCEV(start), loop)) return null;

    for (BasicBlock block : loop.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction == state || instruction == iteration.induction()) continue;
        if (instruction.getUses().stream()
            .anyMatch(use -> !loop.contains(use.getUser().getParent()))) return null;
      }
    }
    return new StateShape(state, start, next);
  }

  private static Integer terminalDomain(Value nextState) {
    Deque<Value> worklist = new ArrayDeque<>();
    Set<Value> visited = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    Set<Integer> domains = new LinkedHashSet<>();
    worklist.add(nextState);
    while (!worklist.isEmpty()) {
      Value value = worklist.removeFirst();
      if (!visited.add(value)) continue;
      if (!(value instanceof Instruction instruction)) return null;
      if (instruction.getOpcode() == Instruction.Opcode.PHI) {
        for (int index = 0; index + 1 < instruction.getNumOperands(); index += 2) {
          worklist.addLast(instruction.getOperand(index));
        }
        continue;
      }
      if (instruction.getOpcode() != Instruction.Opcode.SREM
          || !(instruction.getOperand(1) instanceof Constant.Int modulus)) return null;
      domains.add(ExactI32.normalize(modulus.value));
    }
    return domains.size() == 1 ? domains.iterator().next() : null;
  }

  private record TransitionShape(int instructionCount) {}

  private static TransitionShape validateTransitionCfg(
      LoopAnalysis.Loop loop, IterationShape iteration, StateShape state) {
    Instruction compare = (Instruction) iteration.headerBranch().getOperand(0);
    for (Instruction instruction : loop.header().getInstructions()) {
      if (instruction.getOpcode() == Instruction.Opcode.PHI
          || instruction == compare
          || instruction == iteration.headerBranch()) continue;
      return null;
    }
    Set<BasicBlock> expected = new LinkedHashSet<>();
    for (BasicBlock block : loop.blocks()) {
      if (block != loop.header()) expected.add(block);
    }
    Set<BasicBlock> reachable = reachableTransitionBlocks(iteration.bodyEntry(), loop);
    if (!reachable.equals(expected) || hasCycleBeforeHeader(iteration.bodyEntry(), loop)) return null;

    int instructionCount = 0;
    for (BasicBlock block : reachable) {
      Instruction terminator = block.getTerminator();
      if (terminator == null
          || (terminator.getOpcode() != Instruction.Opcode.BR
              && terminator.getOpcode() != Instruction.Opcode.CONDBR)) return null;
      for (BasicBlock successor : block.getSuccessors()) {
        if (successor == loop.header()) {
          if (block != iteration.latch()) return null;
        } else if (!loop.contains(successor)) {
          return null;
        }
      }
      for (Instruction instruction : block.getInstructions()) {
        if (instruction == terminator || instruction == iteration.inductionNext()) continue;
        if (!isSupportedPureInstruction(instruction)
            || !usesOnlyTransitionState(instruction, loop, iteration, state)) return null;
        if (instruction.getOpcode() != Instruction.Opcode.PHI) instructionCount++;
      }
    }
    return instructionCount == 0 ? null : new TransitionShape(instructionCount);
  }

  private static Set<BasicBlock> reachableTransitionBlocks(
      BasicBlock entry, LoopAnalysis.Loop loop) {
    Set<BasicBlock> reachable = new LinkedHashSet<>();
    Deque<BasicBlock> worklist = new ArrayDeque<>();
    worklist.add(entry);
    while (!worklist.isEmpty()) {
      BasicBlock block = worklist.removeFirst();
      if (block == loop.header() || !loop.contains(block) || !reachable.add(block)) continue;
      for (BasicBlock successor : block.getSuccessors()) {
        if (successor != loop.header()) worklist.addLast(successor);
      }
    }
    return reachable;
  }

  private static boolean hasCycleBeforeHeader(BasicBlock entry, LoopAnalysis.Loop loop) {
    Set<BasicBlock> visiting = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    Set<BasicBlock> visited = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    return hasCycleBeforeHeader(entry, loop, visiting, visited);
  }

  private static boolean hasCycleBeforeHeader(
      BasicBlock block,
      LoopAnalysis.Loop loop,
      Set<BasicBlock> visiting,
      Set<BasicBlock> visited) {
    if (block == loop.header()) return false;
    if (visited.contains(block)) return false;
    if (!visiting.add(block)) return true;
    for (BasicBlock successor : block.getSuccessors()) {
      if (loop.contains(successor)
          && hasCycleBeforeHeader(successor, loop, visiting, visited)) return true;
    }
    visiting.remove(block);
    visited.add(block);
    return false;
  }

  private static boolean isSupportedPureInstruction(Instruction instruction) {
    if (instruction.getOpcode() == Instruction.Opcode.PHI) {
      return instruction.getType() == Type.INT || instruction.getType() == Type.I1;
    }
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR ->
          instruction.getType() == Type.INT
              && instruction.getNumOperands() == 2
              && instruction.getOperand(0).getType() == Type.INT
              && instruction.getOperand(1).getType() == Type.INT;
      case ICMP -> instruction.getType() == Type.I1
          && instruction.getNumOperands() == 2
          && instruction.getOperand(0).getType() == Type.INT
          && instruction.getOperand(1).getType() == Type.INT
          && Set.of("eq", "ne", "slt", "sle", "sgt", "sge")
              .contains(instruction.getPredicate());
      default -> false;
    };
  }

  private static boolean usesOnlyTransitionState(
      Instruction instruction,
      LoopAnalysis.Loop loop,
      IterationShape iteration,
      StateShape state) {
    if (instruction.getOpcode() == Instruction.Opcode.PHI) {
      for (int index = 0; index + 1 < instruction.getNumOperands(); index += 2) {
        Value value = instruction.getOperand(index);
        Value predecessor = instruction.getOperand(index + 1);
        if (!(predecessor instanceof BasicBlock block) || !loop.contains(block)) return false;
        if (!isTransitionValue(value, loop, iteration, state)) return false;
      }
      return true;
    }
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      if (!isTransitionValue(instruction.getOperand(index), loop, iteration, state)) return false;
    }
    return true;
  }

  private static boolean isTransitionValue(
      Value value,
      LoopAnalysis.Loop loop,
      IterationShape iteration,
      StateShape state) {
    if (value instanceof Constant.Int constant) {
      return constant.getType() == Type.INT || constant.getType() == Type.I1;
    }
    if (value == state.state()) return true;
    if (value == iteration.induction() || value == iteration.inductionNext()) return false;
    return value instanceof Instruction operand && loop.contains(operand.getParent());
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
