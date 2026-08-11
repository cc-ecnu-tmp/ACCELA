package accela.pass.ir.transform.loop.fusion;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.SameDomainLoopFusionCandidate;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import accela.pass.ir.analysis.dependence.DependenceAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Legality matcher for strict adjacent, identical-domain loop fusion. */
final class SameDomainLoopFusionMatcher {
  record AdjacentPair(LoopAnalysis.Loop first, LoopAnalysis.Loop second) {}

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

  record Recurrence(Instruction phi, Value initial, Value backedge) {}

  record Forwarding(Instruction store, List<Instruction> loads) {
    Forwarding {
      loads = List.copyOf(loads);
      if (store.getOpcode() != Instruction.Opcode.STORE || loads.isEmpty()) {
        throw new IllegalArgumentException("invalid temporary forwarding plan");
      }
    }
  }

  record Candidate(
      Function function,
      CanonicalLoop first,
      CanonicalLoop second,
      List<Recurrence> secondRecurrences,
      List<Forwarding> forwardings) {
    Candidate {
      secondRecurrences = List.copyOf(secondRecurrences);
      forwardings = List.copyOf(forwardings);
    }
  }

  record CanonicalLoop(
      LoopAnalysis.Loop loop,
      BasicBlock header,
      BasicBlock body,
      BasicBlock exit,
      Instruction branch,
      int exitOperand,
      Instruction compare,
      Instruction induction,
      Instruction nextInduction,
      Value start,
      Value bound,
      String predicate,
      long compareOffset,
      List<BasicBlock> outsidePredecessors,
      List<Instruction> recurrencePhis,
      Set<Instruction> domainInstructions) {
    CanonicalLoop {
      outsidePredecessors = List.copyOf(outsidePredecessors);
      recurrencePhis = List.copyOf(recurrencePhis);
      domainInstructions = Collections.unmodifiableSet(new LinkedHashSet<>(domainInstructions));
    }
  }

  private record IvExpression(long offset, Set<Instruction> instructions) {}

  private record NormalizedCondition(
      Instruction compare,
      Instruction induction,
      Value bound,
      String predicate,
      long offset,
      Set<Instruction> expressionInstructions) {}

  private SameDomainLoopFusionMatcher() {}

  static List<AdjacentPair> findAdjacentPairs(
      Function function, FunctionAnalysisManager analyses) {
    List<LoopAnalysis.Loop> loops =
        analyses.getResult(LoopAnalysis.class, function).loops();
    IdentityHashMap<BasicBlock, LoopAnalysis.Loop> byHeader = new IdentityHashMap<>();
    for (LoopAnalysis.Loop loop : loops) byHeader.put(loop.header(), loop);
    List<AdjacentPair> pairs = new ArrayList<>();
    for (LoopAnalysis.Loop first : loops) {
      Instruction branch = first.header().getTerminator();
      if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR) continue;
      for (int operand = 1; operand <= 2; operand++) {
        if (!(branch.getOperand(operand) instanceof BasicBlock successor)
            || first.contains(successor)) continue;
        LoopAnalysis.Loop second = byHeader.get(successor);
        if (second != null && second != first) pairs.add(new AdjacentPair(first, second));
      }
    }
    return pairs;
  }

  static MatchResult match(
      Function function,
      AdjacentPair pair,
      InductionVariableAnalysis.Result inductionVariables) {
    CanonicalLoop first = canonical(pair.first(), inductionVariables);
    CanonicalLoop second = canonical(pair.second(), inductionVariables);
    if (first == null
        || second == null
        || first.exit() != second.header()
        || second.outsidePredecessors().size() != 1
        || second.outsidePredecessors().getFirst() != first.header()) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.ADJACENT_CANONICAL_LOOPS);
    }
    if (!sameDomain(first, second)) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.IDENTICAL_ITERATION_DOMAIN);
    }
    if (hasOrderSensitiveOperation(first) || hasOrderSensitiveOperation(second)) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.SIDE_EFFECT_ORDER);
    }
    if (!hasSupportedLiveOuts(first, second)) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.EXIT_LIVE_OUTS);
    }

    DependenceAnalysis.FusionSafety memorySafety =
        DependenceAnalysis.classifySequentialFusion(
            first.induction(), List.of(first.body()),
            second.induction(), List.of(second.body()));
    if (memorySafety == DependenceAnalysis.FusionSafety.UNKNOWN_ALIAS_OR_MODREF) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.ALIAS_MODREF);
    }
    if (memorySafety == DependenceAnalysis.FusionSafety.ORDER_VIOLATION) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.DEPENDENCE_ORDER);
    }

    List<Recurrence> recurrences = recurrencePlans(first, second);
    if (recurrences == null) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.EXIT_LIVE_OUTS);
    }
    List<Forwarding> forwardings = temporaryForwardings(function, first, second);
    if (forwardings == null) {
      return MatchResult.rejected(SameDomainLoopFusionCandidate.TEMPORARY_LIFETIME);
    }
    return MatchResult.matched(
        new Candidate(function, first, second, recurrences, forwardings));
  }

  private static CanonicalLoop canonical(
      LoopAnalysis.Loop loop, InductionVariableAnalysis.Result inductionVariables) {
    if (loop.blocks().size() != 2 || loop.latches().size() != 1) return null;
    BasicBlock header = loop.header();
    BasicBlock body = loop.latches().iterator().next();
    if (body == header) return null;
    Instruction bodyBranch = body.getTerminator();
    Instruction branch = header.getTerminator();
    if (bodyBranch == null
        || bodyBranch.getOpcode() != Instruction.Opcode.BR
        || bodyBranch.getOperand(0) != header
        || branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR) return null;
    boolean trueInside = branch.getOperand(1) == body;
    boolean falseInside = branch.getOperand(2) == body;
    if (trueInside == falseInside) return null;
    int exitOperand = trueInside ? 2 : 1;
    BasicBlock exit = (BasicBlock) branch.getOperand(exitOperand);
    if (loop.contains(exit)) return null;

    List<Instruction> phis = header.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
    if (!(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || compare.getParent() != header) return null;

    NormalizedCondition condition = null;
    for (Instruction phi : phis) {
      NormalizedCondition candidate = normalizeCondition(
          compare, phi, loop, trueInside);
      if (candidate == null || !isUnitInduction(phi, body)) continue;
      if (condition != null) return null;
      condition = candidate;
    }
    if (condition == null) return null;
    Instruction induction = condition.induction();
    Instruction next = incomingInstruction(induction, body);
    if (next == null || next.getParent() != body) return null;
    List<BasicBlock> outside = inductionIncomingBlocks(induction, body);
    if (outside.isEmpty() || !sameIncomingStart(induction, outside)) return null;
    Value start = incomingValue(induction, outside.getFirst());

    // Reuse the central IV analysis whenever it represents this canonical single-entry loop. The
    // local proof only extends that exact shape to merged predecessors carrying one identical start.
    boolean represented = inductionVariables.allInductions().stream()
        .anyMatch(item -> item.loop() == loop
            && item.phi() == induction
            && item.next() == next
            && item.step() == 1);
    if (outside.size() == 1 && !represented) return null;

    Set<Instruction> allowedHeader = new LinkedHashSet<>(phis);
    allowedHeader.addAll(condition.expressionInstructions());
    allowedHeader.add(compare);
    allowedHeader.add(branch);
    if (header.getInstructions().stream().anyMatch(inst -> !allowedHeader.contains(inst))) {
      return null;
    }
    List<Instruction> recurrencePhis = phis.stream()
        .filter(phi -> phi != induction)
        .toList();
    if (!validRecurrencePhis(recurrencePhis, body, outside)) return null;
    if (body.getInstructions().stream()
        .anyMatch(inst -> inst.getOpcode() == Instruction.Opcode.PHI)) return null;

    return new CanonicalLoop(
        loop,
        header,
        body,
        exit,
        branch,
        exitOperand,
        compare,
        induction,
        next,
        start,
        condition.bound(),
        condition.predicate(),
        condition.offset(),
        outside,
        recurrencePhis,
        condition.expressionInstructions());
  }

  private static NormalizedCondition normalizeCondition(
      Instruction compare,
      Instruction induction,
      LoopAnalysis.Loop loop,
      boolean trueInside) {
    IvExpression left = ivExpression(compare.getOperand(0), induction, new IdentityHashMap<>());
    IvExpression right = ivExpression(compare.getOperand(1), induction, new IdentityHashMap<>());
    if ((left == null) == (right == null)) return null;
    String predicate = compare.getPredicate();
    Value bound;
    IvExpression expression;
    if (left != null) {
      expression = left;
      bound = compare.getOperand(1);
    } else {
      expression = right;
      bound = compare.getOperand(0);
      predicate = swap(predicate);
    }
    if (!trueInside) predicate = invert(predicate);
    if (!("slt".equals(predicate) || "sle".equals(predicate))
        || (bound instanceof Instruction instruction && loop.contains(instruction.getParent()))) {
      return null;
    }
    return new NormalizedCondition(
        compare, induction, bound, predicate, expression.offset(), expression.instructions());
  }

  private static IvExpression ivExpression(
      Value value, Instruction induction, IdentityHashMap<Value, Boolean> active) {
    if (value == induction) return new IvExpression(0, Set.of());
    if (!(value instanceof Instruction instruction)
        || active.put(value, true) != null
        || instruction.getType() != Type.INT
        || instruction.getNumOperands() != 2
        || (instruction.getOpcode() != Instruction.Opcode.ADD
            && instruction.getOpcode() != Instruction.Opcode.SUB)) return null;
    try {
      for (int constantIndex = 0; constantIndex < 2; constantIndex++) {
        if (!(instruction.getOperand(constantIndex) instanceof Constant.Int constant)
            || constant.getType() != Type.INT) continue;
        int valueIndex = 1 - constantIndex;
        IvExpression nested = ivExpression(instruction.getOperand(valueIndex), induction, active);
        if (nested == null) continue;
        if (instruction.getOpcode() == Instruction.Opcode.SUB && constantIndex != 1) {
          continue;
        }
        long offset;
        try {
          offset = instruction.getOpcode() == Instruction.Opcode.ADD
              ? Math.addExact(nested.offset(), constant.value)
              : Math.subtractExact(nested.offset(), constant.value);
        } catch (ArithmeticException overflow) {
          return null;
        }
        Set<Instruction> instructions = new LinkedHashSet<>(nested.instructions());
        instructions.add(instruction);
        return new IvExpression(offset, instructions);
      }
      return null;
    } finally {
      active.remove(value);
    }
  }

  private static boolean isUnitInduction(Instruction phi, BasicBlock body) {
    Instruction next = incomingInstruction(phi, body);
    if (phi.getType() != Type.INT || next == null || next.getOpcode() != Instruction.Opcode.ADD) {
      return false;
    }
    return (next.getOperand(0) == phi && isIntConstant(next.getOperand(1), 1))
        || (next.getOperand(1) == phi && isIntConstant(next.getOperand(0), 1));
  }

  private static List<BasicBlock> inductionIncomingBlocks(
      Instruction phi, BasicBlock body) {
    List<BasicBlock> result = new ArrayList<>();
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (!(phi.getOperand(index + 1) instanceof BasicBlock block)) return List.of();
      if (block != body) result.add(block);
    }
    return result;
  }

  private static boolean sameIncomingStart(
      Instruction phi, List<BasicBlock> outsidePredecessors) {
    Value first = incomingValue(phi, outsidePredecessors.getFirst());
    if (first == null) return false;
    for (BasicBlock predecessor : outsidePredecessors) {
      if (!equivalentValue(first, incomingValue(phi, predecessor))) return false;
    }
    return true;
  }

  private static boolean validRecurrencePhis(
      List<Instruction> phis, BasicBlock body, List<BasicBlock> outside) {
    for (Instruction phi : phis) {
      if (phi.getType() != Type.INT
          || incomingValue(phi, body) == null
          || !sameIncomingStart(phi, outside)) return false;
    }
    return true;
  }

  private static boolean sameDomain(CanonicalLoop first, CanonicalLoop second) {
    return safePositiveUnitStepDomain(first)
        && safePositiveUnitStepDomain(second)
        && first.predicate().equals(second.predicate())
        && first.compareOffset() == second.compareOffset()
        && equivalentValue(first.start(), second.start())
        && equivalentValue(first.bound(), second.bound());
  }

  private static boolean safePositiveUnitStepDomain(CanonicalLoop loop) {
    if (!"slt".equals(loop.predicate())
        || loop.bound().getType() != Type.INT
        || loop.compareOffset() < 0
        || loop.compareOffset() > Integer.MAX_VALUE) return false;
    if (loop.compareOffset() == 0) return true;
    if (!(loop.start() instanceof Constant.Int start)
        || start.getType() != Type.INT) return false;
    long startValue = (int) start.value;
    long expression = startValue + loop.compareOffset();
    return expression >= Integer.MIN_VALUE && expression <= Integer.MAX_VALUE;
  }

  private static boolean hasOrderSensitiveOperation(CanonicalLoop loop) {
    for (BasicBlock block : List.of(loop.header(), loop.body())) {
      for (Instruction instruction : block.getInstructions()) {
        switch (instruction.getOpcode()) {
          case CALL, ALLOCA, SDIV, SREM,
              FADD, FSUB, FMUL, FDIV, FNEG, FCMP, SITOFP, FPTOSI -> {
            return true;
          }
          default -> {}
        }
      }
    }
    return false;
  }

  private static boolean hasSupportedLiveOuts(
      CanonicalLoop first, CanonicalLoop second) {
    if (second.nextInduction().getUses().stream()
        .anyMatch(use -> use.getUser() != second.induction())) return false;
    if (first.domainInstructions().stream().anyMatch(
        instruction -> instruction.getUses().stream()
            .anyMatch(use -> use.getUser().getParent() != first.header()))) return false;
    if (second.domainInstructions().stream().anyMatch(
        instruction -> instruction.getUses().stream()
            .anyMatch(use -> use.getUser().getParent() != second.header()))) return false;
    if (second.compare().getUses().stream()
        .anyMatch(use -> use.getUser() != second.branch())) return false;
    if (hasEscapingBodyValue(first, first.recurrencePhis())) return false;
    return !hasEscapingBodyValue(second, second.recurrencePhis());
  }

  private static boolean hasEscapingBodyValue(
      CanonicalLoop loop, List<Instruction> recurrencePhis) {
    Set<Instruction> allowedPhis = identitySet(recurrencePhis);
    allowedPhis.add(loop.induction());
    for (Instruction instruction : loop.body().getInstructions()) {
      if (instruction.isTerminator()) continue;
      for (Use use : instruction.getUses()) {
        Instruction user = use.getUser();
        if (loop.loop().contains(user.getParent())) continue;
        if (allowedPhis.contains(user)) continue;
        return true;
      }
    }
    return false;
  }

  private static List<Recurrence> recurrencePlans(
      CanonicalLoop first, CanonicalLoop second) {
    List<Recurrence> result = new ArrayList<>();
    for (Instruction phi : second.recurrencePhis()) {
      Value initial = incomingValue(phi, first.header());
      Value backedge = incomingValue(phi, second.body());
      if (initial == null
          || backedge == null
          || (initial instanceof Instruction instruction
              && first.loop().contains(instruction.getParent()))) return null;
      result.add(new Recurrence(phi, initial, backedge));
    }
    return result;
  }

  private static List<Forwarding> temporaryForwardings(
      Function function, CanonicalLoop first, CanonicalLoop second) {
    List<Instruction> stores = first.body().getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.STORE)
        .toList();
    List<Instruction> loads = second.body().getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.LOAD)
        .toList();
    LinkedHashMap<Instruction, List<Instruction>> byStore = new LinkedHashMap<>();
    for (Instruction load : loads) {
      List<Instruction> matches = stores.stream()
          .filter(store -> store.getOperand(0).getType() == load.getType())
          .filter(store -> DependenceAnalysis.isSameIterationAddress(
              store.getOperand(1), first.induction(),
              load.getOperand(0), second.induction()))
          .toList();
      if (matches.size() > 1) return null;
      if (matches.size() == 1) {
        byStore.computeIfAbsent(matches.getFirst(), ignored -> new ArrayList<>()).add(load);
      }
    }
    if (byStore.isEmpty()) return List.of();
    accela.ir.Module module = function.getModule();
    if (module == null) return null;
    GlobalModRefAnalysis.Result modRef = GlobalModRefAnalysis.analyze(module);
    List<Forwarding> result = new ArrayList<>();
    for (var entry : byStore.entrySet()) {
      Instruction store = entry.getKey();
      Value root = PointerProvenance.root(store.getOperand(1));
      if (!(root instanceof GlobalVariable) && !isAlloca(root)) return null;
      Set<Instruction> selectedLoads = identitySet(entry.getValue());
      long storesInProducer = first.body().getInstructions().stream()
          .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.STORE)
          .filter(instruction -> PointerProvenance.root(instruction.getOperand(1)) == root)
          .count();
      if (storesInProducer != 1) return null;
      for (Function owner : module.getFunctions()) {
        for (BasicBlock block : owner.getBlocks()) {
          for (Instruction instruction : block.getInstructions()) {
            if (instruction.getOpcode() == Instruction.Opcode.LOAD
                && PointerProvenance.root(instruction.getOperand(0)) == root
                && !selectedLoads.contains(instruction)) return null;
            if (instruction.getOpcode() == Instruction.Opcode.STORE
                && PointerProvenance.root(instruction.getOperand(1)) == root
                && instruction.getParent() == second.body()) return null;
            if (instruction.getOpcode() == Instruction.Opcode.CALL
                && modRef.mayRead(instruction, root)) return null;
            if (pointerEscapes(instruction, root)) return null;
          }
        }
      }
      result.add(new Forwarding(store, entry.getValue()));
    }
    return result;
  }

  private static boolean pointerEscapes(Instruction instruction, Value root) {
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      Value operand = instruction.getOperand(index);
      if (!operand.getType().isPointer() || PointerProvenance.root(operand) != root) continue;
      boolean allowed = switch (instruction.getOpcode()) {
        case GEP -> index == 0;
        case LOAD -> index == 0;
        case STORE -> index == 1;
        default -> false;
      };
      if (!allowed) return true;
    }
    return false;
  }

  private static boolean isAlloca(Value value) {
    return value instanceof Instruction instruction
        && instruction.getOpcode() == Instruction.Opcode.ALLOCA;
  }

  private static Instruction incomingInstruction(Instruction phi, BasicBlock predecessor) {
    Value value = incomingValue(phi, predecessor);
    return value instanceof Instruction instruction ? instruction : null;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index + 1 < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static boolean equivalentValue(Value left, Value right) {
    if (left == right) return true;
    if (left instanceof Constant.Int leftInt && right instanceof Constant.Int rightInt) {
      return left.getType() == right.getType() && leftInt.value == rightInt.value;
    }
    return false;
  }

  private static boolean isIntConstant(Value value, long expected) {
    return value instanceof Constant.Int constant && constant.value == expected;
  }

  private static String invert(String predicate) {
    return switch (predicate) {
      case "slt" -> "sge";
      case "sle" -> "sgt";
      case "sgt" -> "sle";
      case "sge" -> "slt";
      case "eq" -> "ne";
      case "ne" -> "eq";
      default -> "";
    };
  }

  private static String swap(String predicate) {
    return switch (predicate) {
      case "slt" -> "sgt";
      case "sle" -> "sge";
      case "sgt" -> "slt";
      case "sge" -> "sle";
      case "eq", "ne" -> predicate;
      default -> "";
    };
  }

  private static <T> Set<T> identitySet(List<T> values) {
    Set<T> result = Collections.newSetFromMap(new IdentityHashMap<>());
    result.addAll(values);
    return result;
  }
}
