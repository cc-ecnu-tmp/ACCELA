package accela.pass.ir.transform.loop.unroll;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;

/** A canonical two-level nest, optionally preceded by a side-effect-free per-lane guard. */
record LoopUnrollAndJamCandidate(
    InductionVariableAnalysis.Induction outerInduction,
    InductionVariableAnalysis.Induction innerInduction,
    BasicBlock outerHeader,
    BasicBlock outerPreheader,
    BasicBlock laneGuard,
    BasicBlock innerCondition,
    BasicBlock innerPreheader,
    BasicBlock innerBody,
    BasicBlock innerExit,
    Value outerBound,
    String outerPredicate) {

  static List<LoopUnrollAndJamCandidate> findAll(
      accela.ir.Function function, FunctionAnalysisManager fam) {
    var loops = fam.getResult(LoopAnalysis.class, function).loops();
    var inductions =
        fam.getResult(InductionVariableAnalysis.class, function).allInductions();
    List<LoopUnrollAndJamCandidate> candidates = new ArrayList<>();
    for (LoopAnalysis.Loop outer : loops) {
      List<LoopAnalysis.Loop> children = directChildren(outer, loops);
      if (children.size() != 1) continue;
      var candidate = match(outer, children.getFirst(), inductions);
      if (candidate != null) candidates.add(candidate);
    }
    return candidates;
  }

  private static LoopUnrollAndJamCandidate match(
      LoopAnalysis.Loop outer,
      LoopAnalysis.Loop inner,
      List<InductionVariableAnalysis.Induction> inductions) {
    if (outer.latches().size() != 1
        || inner.preheader() == null
        || inner.latches().size() != 1) {
      return null;
    }
    var outerInduction = uniqueInduction(outer, inductions);
    var innerInduction = uniqueInduction(inner, inductions);
    if (outerInduction == null || innerInduction == null) return null;
    BasicBlock outerPreheader = outerInduction.predecessor();

    BasicBlock outerHeader = outer.header();
    BasicBlock innerBody = inner.header();
    BasicBlock innerExit = outer.latches().iterator().next();
    Instruction outerBranch = outerHeader.getTerminator();
    Instruction innerBranch = innerBody.getTerminator();
    if (!isConditional(outerBranch) || !isConditional(innerBranch)
        || !branchesToBoth(innerBranch, innerBody, innerExit)
        || !isUnconditionalBranch(innerExit.getTerminator(), outerHeader)) return null;

    BasicBlock outerBody = insideSuccessor(outerBranch, outer);
    BasicBlock innerPreheader = inner.preheader();
    if (outerBody == null
        || !isUnconditionalBranch(innerPreheader.getTerminator(), innerBody)
        || innerPreheader.getPredecessors().size() != 1) return null;

    BasicBlock innerCondition = innerPreheader.getPredecessors().getFirst();
    Instruction guard = innerCondition.getTerminator();
    BasicBlock laneGuard = outerBody == innerCondition ? null : outerBody;
    if (!isConditional(guard)
        || !branchesToBoth(guard, innerPreheader, innerExit)
        || innerCondition.getPredecessors().size() != 1
        || innerCondition.getPredecessors().getFirst()
            != (laneGuard == null ? outerHeader : laneGuard)) return null;
    if (laneGuard != null
        && (!isConditional(laneGuard.getTerminator())
            || !branchesToBoth(laneGuard.getTerminator(), innerCondition, innerExit)
            || laneGuard.getPredecessors().size() != 1
            || laneGuard.getPredecessors().getFirst() != outerHeader
            || containsStoreOrCall(laneGuard))) return null;

    Instruction compare =
        outerBranch.getOperand(0) instanceof Instruction instruction
            && instruction.getOpcode() == Instruction.Opcode.ICMP
            ? instruction
            : null;
    NormalizedCompare normalized =
        normalize(compare, outerInduction.phi(), outerBranch.getOperand(1) == outerBody);
    if (normalized == null || !stepMatches(normalized.predicate(), outerInduction.step())
        || !isIntegerType(outerInduction.phi().getType())
        || normalized.bound().getType() != outerInduction.phi().getType()
        || !isInvariant(normalized.bound(), outer)
        || outerInduction.next().getUses().stream()
            .anyMatch(use -> use.getUser() != outerInduction.phi())) return null;

    if (!hasOnlyInductionPhi(outerHeader, outerInduction.phi(), compare)
        || !hasCanonicalPhis(innerBody, innerPreheader, innerBody)
        || !hasCanonicalPhis(innerExit, innerCondition, innerBody)
        || containsCall(outer)
        || containsMemory(innerCondition)
        || hasLaneVariantWork(innerCondition, outerInduction.phi())
        || innerExitConditionVariesByLane(
            innerBranch.getOperand(0), outerInduction.phi(), innerInduction.phi(), innerBody)) {
      return null;
    }

    return new LoopUnrollAndJamCandidate(
        outerInduction, innerInduction, outerHeader, outerPreheader,
        laneGuard, innerCondition, innerPreheader, innerBody, innerExit,
        normalized.bound(), normalized.predicate());
  }

  private static List<LoopAnalysis.Loop> directChildren(
      LoopAnalysis.Loop outer, List<LoopAnalysis.Loop> loops) {
    List<LoopAnalysis.Loop> nested = loops.stream()
        .filter(loop -> loop != outer && outer.contains(loop.header()))
        .toList();
    List<LoopAnalysis.Loop> direct = new ArrayList<>();
    for (LoopAnalysis.Loop loop : nested) {
      boolean hasParentBetween = nested.stream().anyMatch(parent ->
          parent != loop && parent.contains(loop.header())
              && outer.contains(parent.header()));
      if (!hasParentBetween) direct.add(loop);
    }
    return direct;
  }

  private static InductionVariableAnalysis.Induction uniqueInduction(
      LoopAnalysis.Loop loop,
      List<InductionVariableAnalysis.Induction> inductions) {
    List<InductionVariableAnalysis.Induction> matches =
        inductions.stream().filter(induction -> induction.loop() == loop).toList();
    return matches.size() == 1 ? matches.getFirst() : null;
  }

  private static NormalizedCompare normalize(
      Instruction compare, Instruction induction, boolean bodyOnTrueEdge) {
    if (compare == null || compare.getNumOperands() != 2) return null;
    String predicate = compare.getPredicate();
    Value bound;
    if (compare.getOperand(0) == induction) {
      bound = compare.getOperand(1);
    } else if (compare.getOperand(1) == induction) {
      predicate = swapped(predicate);
      bound = compare.getOperand(0);
    } else {
      return null;
    }
    if (!bodyOnTrueEdge) predicate = inverted(predicate);
    return predicate == null ? null : new NormalizedCompare(predicate, bound);
  }

  private static String swapped(String predicate) {
    return switch (predicate) {
      case "slt" -> "sgt";
      case "sle" -> "sge";
      case "sgt" -> "slt";
      case "sge" -> "sle";
      default -> null;
    };
  }

  private static String inverted(String predicate) {
    return switch (predicate) {
      case "slt" -> "sge";
      case "sle" -> "sgt";
      case "sgt" -> "sle";
      case "sge" -> "slt";
      default -> null;
    };
  }

  private static boolean stepMatches(String predicate, long step) {
    return switch (predicate) {
      case "slt", "sle" -> step > 0;
      case "sgt", "sge" -> step < 0;
      default -> false;
    };
  }

  private static boolean hasOnlyInductionPhi(
      BasicBlock header, Instruction induction, Instruction compare) {
    List<Instruction> instructions = header.getInstructions();
    return instructions.size() == 3
        && instructions.getFirst() == induction
        && instructions.get(1) == compare;
  }

  private static boolean hasLaneVariantWork(
      BasicBlock block, Instruction outerInduction) {
    return block.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() != Instruction.Opcode.PHI
            && !instruction.isTerminator())
        .anyMatch(instruction -> dependsOn(instruction, outerInduction));
  }

  private static boolean hasCanonicalPhis(
      BasicBlock block, BasicBlock firstPredecessor, BasicBlock secondPredecessor) {
    for (Instruction phi : block.getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      if (incomingValue(phi, firstPredecessor) == null
          || incomingValue(phi, secondPredecessor) == null
          || phi.getNumOperands() != 4) return false;
    }
    return true;
  }

  private static boolean innerExitConditionVariesByLane(
      Value condition,
      Instruction outerInduction,
      Instruction innerInduction,
      BasicBlock innerBody) {
    Set<Value> forbidden = Collections.newSetFromMap(new IdentityHashMap<>());
    forbidden.add(outerInduction);
    innerBody.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .filter(phi -> phi != innerInduction)
        .forEach(forbidden::add);
    return dependsOnAny(condition, forbidden, Collections.newSetFromMap(new IdentityHashMap<>()));
  }

  static boolean dependsOn(Value value, Value dependency) {
    return dependsOnAny(
        value, Set.of(dependency), Collections.newSetFromMap(new IdentityHashMap<>()));
  }

  private static boolean dependsOnAny(Value value, Set<Value> dependencies, Set<Value> visited) {
    if (dependencies.contains(value)) return true;
    if (!(value instanceof Instruction instruction) || !visited.add(value)) return false;
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      if (dependsOnAny(instruction.getOperand(index), dependencies, visited)) return true;
    }
    return false;
  }

  private static boolean containsCall(LoopAnalysis.Loop loop) {
    return loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
  }

  private static boolean containsMemory(BasicBlock block) {
    return block.getInstructions().stream().anyMatch(instruction ->
        instruction.getOpcode() == Instruction.Opcode.LOAD
            || instruction.getOpcode() == Instruction.Opcode.STORE);
  }

  private static boolean isIntegerType(accela.ir.Type type) {
    return type == accela.ir.Type.INT || type == accela.ir.Type.I64;
  }

  private static boolean containsStoreOrCall(BasicBlock block) {
    return block.getInstructions().stream().anyMatch(instruction ->
        instruction.getOpcode() == Instruction.Opcode.STORE
            || instruction.getOpcode() == Instruction.Opcode.CALL);
  }

  private static boolean isInvariant(Value value, LoopAnalysis.Loop loop) {
    return !(value instanceof Instruction instruction) || !loop.contains(instruction.getParent());
  }

  static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static boolean isConditional(Instruction instruction) {
    return instruction != null && instruction.getOpcode() == Instruction.Opcode.CONDBR;
  }

  private static boolean isUnconditionalBranch(
      Instruction instruction, BasicBlock target) {
    return instruction != null && instruction.getOpcode() == Instruction.Opcode.BR
        && instruction.getOperand(0) == target;
  }

  private static boolean branchesToBoth(
      Instruction branch, BasicBlock first, BasicBlock second) {
    return branch.getOperand(1) == first && branch.getOperand(2) == second
        || branch.getOperand(1) == second && branch.getOperand(2) == first;
  }

  private static BasicBlock insideSuccessor(
      Instruction branch, LoopAnalysis.Loop loop) {
    BasicBlock first = (BasicBlock) branch.getOperand(1);
    BasicBlock second = (BasicBlock) branch.getOperand(2);
    return loop.contains(first) != loop.contains(second)
        ? (loop.contains(first) ? first : second)
        : null;
  }

  private record NormalizedCompare(String predicate, Value bound) {}
}
