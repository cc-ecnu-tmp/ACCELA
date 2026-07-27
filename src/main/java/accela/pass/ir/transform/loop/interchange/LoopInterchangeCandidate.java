package accela.pass.ir.transform.loop.interchange;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.dependence.DependenceAnalysis;
import java.util.ArrayList;
import java.util.List;

/** A tightly nested, top-tested pair of loops supported by LoopInterchange. */
record LoopInterchangeCandidate(
    InductionVariableAnalysis.Induction outerInduction,
    InductionVariableAnalysis.Induction innerInduction,
    BasicBlock outerHeader,
    BasicBlock innerHeader,
    BasicBlock innerBody,
    BasicBlock outerLatch,
    Instruction outerCompare,
    Instruction innerCompare,
    Value outerBound,
    Value innerBound,
    DependenceAnalysis.Result dependences) {

  static List<LoopInterchangeCandidate> findAll(
      Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops =
        fam.getResult(LoopAnalysis.class, function).loops();
    List<InductionVariableAnalysis.Induction> inductions =
        fam.getResult(InductionVariableAnalysis.class, function).allInductions();
    List<LoopInterchangeCandidate> candidates = new ArrayList<>();
    for (LoopAnalysis.Loop outer : loops) {
      List<LoopAnalysis.Loop> children = directChildren(outer, loops);
      if (children.size() != 1 || !directChildren(children.getFirst(), loops).isEmpty()) {
        continue;
      }
      LoopInterchangeCandidate candidate =
          match(outer, children.getFirst(), inductions);
      if (candidate != null) candidates.add(candidate);
    }
    return candidates;
  }

  private static LoopInterchangeCandidate match(
      LoopAnalysis.Loop outer,
      LoopAnalysis.Loop inner,
      List<InductionVariableAnalysis.Induction> inductions) {
    var outerInduction = uniqueInduction(outer, inductions);
    var innerInduction = uniqueInduction(inner, inductions);
    if (outerInduction == null
        || innerInduction == null
        || outer.latches().size() != 1
        || inner.latches().size() != 1) return null;

    BasicBlock outerHeader = outer.header();
    BasicBlock innerHeader = inner.header();
    BasicBlock innerBody = inner.latches().iterator().next();
    BasicBlock outerLatch = outer.latches().iterator().next();
    Instruction outerBranch = outerHeader.getTerminator();
    Instruction innerBranch = innerHeader.getTerminator();
    if (!isConditionalTo(outerBranch, innerHeader)
        || !isConditionalTo(innerBranch, innerBody)
        || !isBranchTo(innerBody.getTerminator(), innerHeader)
        || !isBranchTo(outerLatch.getTerminator(), outerHeader)
        || innerBranch.getOperand(2) != outerLatch) return null;

    Instruction outerCompare = compareFor(outerBranch, outerInduction);
    Instruction innerCompare = compareFor(innerBranch, innerInduction);
    if (outerCompare == null
        || innerCompare == null
        || !canonicalHeader(outerHeader, outerInduction.phi(), outerCompare)
        || !canonicalHeader(innerHeader, innerInduction.phi(), innerCompare)
        || !canonicalLatch(outerLatch, outerInduction.next())
        || !canonicalBody(innerBody, innerInduction.next())
        || !canonicalNext(outerInduction)
        || !canonicalNext(innerInduction)
        || !isInvariant(outerInduction.start(), outer)
        || !isInvariant(innerInduction.start(), outer)
        || !isInvariant(outerCompare.getOperand(1), outer)
        || !isInvariant(innerCompare.getOperand(1), outer)
        || containsCall(outer)
        || hasUnsupportedUses(outerInduction, innerBody, outerCompare)
        || hasUnsupportedUses(innerInduction, innerBody, innerCompare)
        || hasEscapingBodyValue(innerBody, outer)) {
      return null;
    }

    DependenceAnalysis.Result dependences = DependenceAnalysis.analyze(
        List.of(outerInduction.phi(), innerInduction.phi()),
        List.of(innerBody));
    if (!dependences.isLegalToInterchange(0, 1)) return null;
    return new LoopInterchangeCandidate(
        outerInduction,
        innerInduction,
        outerHeader,
        innerHeader,
        innerBody,
        outerLatch,
        outerCompare,
        innerCompare,
        outerCompare.getOperand(1),
        innerCompare.getOperand(1),
        dependences);
  }

  private static List<LoopAnalysis.Loop> directChildren(
      LoopAnalysis.Loop outer, List<LoopAnalysis.Loop> loops) {
    List<LoopAnalysis.Loop> nested = loops.stream()
        .filter(loop -> loop != outer && outer.contains(loop.header()))
        .toList();
    return nested.stream()
        .filter(loop -> nested.stream().noneMatch(parent ->
            parent != loop && parent.contains(loop.header())))
        .toList();
  }

  private static InductionVariableAnalysis.Induction uniqueInduction(
      LoopAnalysis.Loop loop,
      List<InductionVariableAnalysis.Induction> inductions) {
    List<InductionVariableAnalysis.Induction> matches =
        inductions.stream().filter(induction -> induction.loop() == loop).toList();
    return matches.size() == 1 ? matches.getFirst() : null;
  }

  private static Instruction compareFor(
      Instruction branch, InductionVariableAnalysis.Induction induction) {
    if (branch.getOperand(0) instanceof Instruction compare
        && compare.getOpcode() == Instruction.Opcode.ICMP
        && compare.getNumOperands() == 2
        && compare.getOperand(0) == induction.phi()
        && stepMatches(compare.getPredicate(), induction.step())) {
      return compare;
    }
    return null;
  }

  private static boolean stepMatches(String predicate, long step) {
    return switch (predicate) {
      case "slt", "sle" -> step > 0;
      case "sgt", "sge" -> step < 0;
      default -> false;
    };
  }

  private static boolean canonicalHeader(
      BasicBlock header, Instruction induction, Instruction compare) {
    List<Instruction> instructions = header.getInstructions();
    return instructions.size() == 3
        && instructions.getFirst() == induction
        && instructions.get(1) == compare;
  }

  private static boolean canonicalLatch(BasicBlock latch, Instruction next) {
    return latch.getInstructions().size() == 2
        && latch.getInstructions().getFirst() == next;
  }

  private static boolean canonicalBody(BasicBlock body, Instruction next) {
    return body.getInstructions().stream()
            .noneMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        && next.getParent() == body;
  }

  private static boolean canonicalNext(InductionVariableAnalysis.Induction induction) {
    Instruction next = induction.next();
    return next.getOpcode() == Instruction.Opcode.ADD
        && next.getNumOperands() == 2
        && next.getOperand(0) == induction.phi()
        && next.getOperand(1) instanceof Constant.Int
        && next.getUses().stream().allMatch(use -> use.getUser() == induction.phi());
  }

  private static boolean hasUnsupportedUses(
      InductionVariableAnalysis.Induction induction,
      BasicBlock body,
      Instruction compare) {
    for (Use use : induction.phi().getUses()) {
      Instruction user = use.getUser();
      if (user == compare || user == induction.next()) continue;
      if (user.getParent() != body) return true;
    }
    return false;
  }

  private static boolean hasEscapingBodyValue(
      BasicBlock body, LoopAnalysis.Loop outer) {
    for (Instruction instruction : body.getInstructions()) {
      if (instruction.isTerminator()) continue;
      if (instruction.getUses().stream()
          .anyMatch(use -> !outer.contains(use.getUser().getParent()))) return true;
    }
    return false;
  }

  private static boolean containsCall(LoopAnalysis.Loop loop) {
    return loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == Instruction.Opcode.CALL);
  }

  private static boolean isInvariant(Value value, LoopAnalysis.Loop loop) {
    return !(value instanceof Instruction instruction)
        || !loop.contains(instruction.getParent());
  }

  private static boolean isConditionalTo(Instruction branch, BasicBlock trueTarget) {
    return branch != null
        && branch.getOpcode() == Instruction.Opcode.CONDBR
        && branch.getOperand(1) == trueTarget;
  }

  private static boolean isBranchTo(Instruction branch, BasicBlock target) {
    return branch != null
        && branch.getOpcode() == Instruction.Opcode.BR
        && branch.getOperand(0) == target;
  }
}
