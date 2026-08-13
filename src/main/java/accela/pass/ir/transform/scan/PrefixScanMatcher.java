package accela.pass.ir.transform.scan;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import accela.pass.ir.analysis.dependence.DependenceAnalysis;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Structural and semantic matcher for one family of repeated integer scans. */
final class PrefixScanMatcher {
  record Assessment(PrefixScanCandidate candidate, String rejectedObligation, BasicBlock target) {
    Assessment {
      if ((candidate == null) == (rejectedObligation == null)) {
        throw new IllegalArgumentException(
            "assessment must contain exactly one candidate or rejection obligation");
      }
    }

    static Assessment accepted(PrefixScanCandidate candidate) {
      return new Assessment(candidate, null, candidate.outerHeader());
    }

    static Assessment rejected(String obligation, BasicBlock target) {
      return new Assessment(null, obligation, target);
    }
  }

  private record AddressMatch(Value root, Set<Instruction> instructions) {}

  private static final class TermMatch {
    final Set<Instruction> instructions = new LinkedHashSet<>();
    final List<AddressMatch> addresses = new ArrayList<>();
    final Set<Value> visiting = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    boolean outerDependent;
    boolean unsupported;
  }

  private PrefixScanMatcher() {}

  static List<Assessment> assess(Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops = fam.getResult(LoopAnalysis.class, function).loops();
    List<InductionVariableAnalysis.Induction> inductions =
        fam.getResult(InductionVariableAnalysis.class, function).allInductions();
    List<Assessment> assessments = new ArrayList<>();
    for (LoopAnalysis.Loop outer : loops) {
      List<LoopAnalysis.Loop> children = directChildren(outer, loops);
      if (children.isEmpty()) continue;
      if (children.size() != 1) {
        assessments.add(Assessment.rejected(
            PrefixScanReuse.REPEATED_PREFIX_DOMAIN, outer.header()));
        continue;
      }
      LoopAnalysis.Loop inner = children.getFirst();
      if (!directChildren(inner, loops).isEmpty()) {
        assessments.add(Assessment.rejected(
            PrefixScanReuse.REPEATED_PREFIX_DOMAIN, outer.header()));
        continue;
      }
      assessments.add(match(outer, inner, inductions));
    }
    return List.copyOf(assessments);
  }

  private static Assessment match(
      LoopAnalysis.Loop outer,
      LoopAnalysis.Loop inner,
      List<InductionVariableAnalysis.Induction> inductions) {
    BasicBlock target = outer.header();
    var outerIV = uniqueInduction(outer, inductions);
    var innerIV = uniqueInduction(inner, inductions);
    if (outerIV == null
        || innerIV == null
        || innerIV.predecessor() != outer.header()
        || outer.latches().size() != 1
        || inner.latches().size() != 1
        || outer.blocks().size() != 4
        || inner.blocks().size() != 2) {
      return Assessment.rejected(PrefixScanReuse.REPEATED_PREFIX_DOMAIN, target);
    }

    BasicBlock outerHeader = outer.header();
    BasicBlock innerHeader = inner.header();
    BasicBlock innerBody = inner.latches().iterator().next();
    BasicBlock outerLatch = outer.latches().iterator().next();
    Instruction outerBranch = outerHeader.getTerminator();
    Instruction innerBranch = innerHeader.getTerminator();
    if (!isConditionalTo(outerBranch, innerHeader)
        || !isConditionalTo(innerBranch, innerBody)
        || innerBranch.getOperand(2) != outerLatch
        || !isBranchTo(innerBody.getTerminator(), innerHeader)
        || !isBranchTo(outerLatch.getTerminator(), outerHeader)
        || !canonicalHeader(outerHeader, 1)
        || !canonicalHeader(innerHeader, 2)) {
      return Assessment.rejected(PrefixScanReuse.REPEATED_PREFIX_DOMAIN, target);
    }

    Instruction outerCompare = compareFor(outerBranch, outerIV.phi());
    if (outerCompare == null
        || outerIV.step() != 1
        || !isI32Zero(outerIV.start())
        || !"slt".equals(outerCompare.getPredicate())
        || outerCompare.getOperand(0) != outerIV.phi()
        || outerCompare.getOperand(1).getType() != Type.INT
        || definedInside(outerCompare.getOperand(1), outer)) {
      return Assessment.rejected(PrefixScanReuse.EMPTY_DOMAIN, target);
    }
    Value outerBound = outerCompare.getOperand(1);

    Instruction innerCompare = compareFor(innerBranch, innerIV.phi());
    PrefixScanCandidate.Kind kind = classifyDomain(
        innerIV, innerCompare, outerIV.phi(), outerBound);
    if (kind == null) {
      return Assessment.rejected(PrefixScanReuse.REPEATED_PREFIX_DOMAIN, target);
    }

    List<Instruction> innerPhis = leadingPhis(innerHeader);
    if (innerPhis.size() != 2 || !innerPhis.contains(innerIV.phi())) {
      return Assessment.rejected(PrefixScanReuse.INTEGER_ORDER, target);
    }
    Instruction reduction = innerPhis.getFirst() == innerIV.phi()
        ? innerPhis.get(1) : innerPhis.getFirst();
    Value reductionStart = incomingValue(reduction, outerHeader);
    Value reductionBackedge = incomingValue(reduction, innerBody);
    if (reduction.getType() != Type.INT
        || !isI32Zero(reductionStart)
        || !(reductionBackedge instanceof Instruction reductionUpdate)
        || reductionUpdate.getParent() != innerBody
        || reductionUpdate.getOpcode() != Instruction.Opcode.ADD
        || reductionUpdate.getType() != Type.INT
        || reductionUpdate.getNumOperands() != 2
        || !containsIdentity(reductionUpdate, reduction)) {
      return Assessment.rejected(PrefixScanReuse.INTEGER_ORDER, target);
    }
    Value term = reductionUpdate.getOperand(
        reductionUpdate.getOperand(0) == reduction ? 1 : 0);

    if (containsOpcode(innerBody, Instruction.Opcode.CALL)
        || containsOpcode(innerBody, Instruction.Opcode.STORE)
        || containsOpcode(outerLatch, Instruction.Opcode.CALL)) {
      return Assessment.rejected(PrefixScanReuse.SIDE_EFFECT_FREE_KERNEL, target);
    }
    TermMatch termMatch = new TermMatch();
    collectTerm(term, innerIV.phi(), outerIV.phi(), outer, innerBody, termMatch);
    if (termMatch.outerDependent) {
      return Assessment.rejected(PrefixScanReuse.INCREMENTAL_EQUIVALENCE, target);
    }
    if (termMatch.unsupported || termMatch.addresses.isEmpty()) {
      return Assessment.rejected(PrefixScanReuse.SIDE_EFFECT_FREE_KERNEL, target);
    }

    Set<Instruction> allowedInner = new LinkedHashSet<>(termMatch.instructions);
    allowedInner.add(reductionUpdate);
    allowedInner.add(innerIV.next());
    allowedInner.add(innerBody.getTerminator());
    if (!allowedInner.containsAll(innerBody.getInstructions())
        || !innerBody.getInstructions().containsAll(allowedInner)) {
      return Assessment.rejected(PrefixScanReuse.SIDE_EFFECT_FREE_KERNEL, target);
    }

    List<Instruction> stores = outerLatch.getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.STORE)
        .toList();
    if (stores.size() != 1) {
      return Assessment.rejected(PrefixScanReuse.SIDE_EFFECT_FREE_KERNEL, target);
    }
    Instruction outputStore = stores.getFirst();
    if (outputStore.getOperand(0) != reduction) {
      return Assessment.rejected(PrefixScanReuse.LIVE_OUTS, target);
    }
    AddressMatch outputAddress = matchUnitStrideAddress(
        outputStore.getOperand(1), outerIV.phi(), outerLatch, outer);
    if (outputAddress == null) {
      return Assessment.rejected(PrefixScanReuse.BOUNDS, target);
    }

    Set<Instruction> allowedLatch = new LinkedHashSet<>(outputAddress.instructions());
    allowedLatch.add(outputStore);
    allowedLatch.add(outerIV.next());
    allowedLatch.add(outerLatch.getTerminator());
    if (!allowedLatch.containsAll(outerLatch.getInstructions())
        || !outerLatch.getInstructions().containsAll(allowedLatch)) {
      return Assessment.rejected(PrefixScanReuse.SIDE_EFFECT_FREE_KERNEL, target);
    }

    for (AddressMatch source : termMatch.addresses) {
      if (PointerProvenance.mayAlias(source.root(), outputAddress.root())) {
        return Assessment.rejected(PrefixScanReuse.ALIAS_MODREF, target);
      }
    }
    DependenceAnalysis.Result dependences = DependenceAnalysis.analyze(
        List.of(outerIV.phi(), innerIV.phi()), List.of(innerBody));
    if (!dependences.isLegalToInterchange(0, 1)) {
      return Assessment.rejected(PrefixScanReuse.ALIAS_MODREF, target);
    }

    if (!validLiveOuts(inner, reduction, outputStore)
        || outerIV.next().getUses().stream()
            .anyMatch(use -> use.getUser() != outerIV.phi())) {
      return Assessment.rejected(PrefixScanReuse.LIVE_OUTS, target);
    }

    PrefixScanCandidate candidate = new PrefixScanCandidate(
        kind,
        inner,
        outerIV.predecessor(),
        outerHeader,
        innerHeader,
        innerBody,
        outerLatch,
        outerIV.phi(),
        outerBound,
        innerIV.phi(),
        reduction,
        reductionUpdate,
        reductionStart,
        term,
        outputStore,
        outputStore.getOperand(1),
        termMatch.instructions,
        outputAddress.instructions());
    if (!PrefixScanProfitability.isProfitable(candidate)) {
      return Assessment.rejected(PrefixScanReuse.PROFITABILITY, target);
    }
    return Assessment.accepted(candidate);
  }

  private static PrefixScanCandidate.Kind classifyDomain(
      InductionVariableAnalysis.Induction innerIV,
      Instruction compare,
      Instruction outerIV,
      Value outerBound) {
    if (compare == null || innerIV.step() != 1 || compare.getOperand(0) != innerIV.phi()) {
      return null;
    }
    if (isI32Zero(innerIV.start())
        && "sle".equals(compare.getPredicate())
        && compare.getOperand(1) == outerIV) {
      return PrefixScanCandidate.Kind.FORWARD_PREFIX;
    }
    if (innerIV.start() == outerIV
        && "slt".equals(compare.getPredicate())
        && sameI32Value(compare.getOperand(1), outerBound)) {
      return PrefixScanCandidate.Kind.REVERSE_SUFFIX;
    }
    return null;
  }

  private static void collectTerm(
      Value value,
      Instruction innerIV,
      Instruction outerIV,
      LoopAnalysis.Loop outer,
      BasicBlock innerBody,
      TermMatch result) {
    if (value == innerIV || value instanceof Constant.Int) return;
    if (value == outerIV) {
      result.outerDependent = true;
      return;
    }
    if (!(value instanceof Instruction instruction)) return;
    if (!result.visiting.add(instruction)) {
      result.unsupported = true;
      return;
    }
    try {
      if (instruction.getParent() != innerBody) {
        if (outer.contains(instruction.getParent())) result.outerDependent = true;
        else result.unsupported = true;
        return;
      }
      if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
        if (instruction.getType() != Type.INT || instruction.getNumOperands() != 1) {
          result.unsupported = true;
          return;
        }
        AddressMatch address = matchUnitStrideAddress(
            instruction.getOperand(0), innerIV, innerBody, outer);
        if (address == null) {
          result.unsupported = true;
          return;
        }
        result.addresses.add(address);
        result.instructions.addAll(address.instructions());
        result.instructions.add(instruction);
        return;
      }
      if (!isPureI32TermOpcode(instruction.getOpcode())
          || instruction.getType() != Type.INT
          || instruction.getNumOperands() != 2) {
        result.unsupported = true;
        return;
      }
      for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
        collectTerm(
            instruction.getOperand(operand), innerIV, outerIV, outer, innerBody, result);
      }
      result.instructions.add(instruction);
    } finally {
      result.visiting.remove(instruction);
    }
  }

  private static boolean isPureI32TermOpcode(Instruction.Opcode opcode) {
    return switch (opcode) {
      case ADD, SUB, MUL, SHL, ASHR, AND, XOR -> true;
      default -> false;
    };
  }

  private static AddressMatch matchUnitStrideAddress(
      Value pointer,
      Instruction induction,
      BasicBlock expectedBlock,
      LoopAnalysis.Loop outer) {
    if (!(pointer instanceof Instruction gep)
        || gep.getOpcode() != Instruction.Opcode.GEP
        || gep.getParent() != expectedBlock
        || !gep.isGepInbounds()
        || gep.getNumOperands() < 2) return null;
    Value root = PointerProvenance.root(pointer);
    if (root instanceof Instruction rootInstruction && outer.contains(rootInstruction.getParent())) {
      return null;
    }

    int inductionIndices = 0;
    Set<Instruction> instructions = new LinkedHashSet<>();
    instructions.add(gep);
    for (int index = 1; index < gep.getNumOperands(); index++) {
      Value subscript = gep.getOperand(index);
      Value stripped = stripIntegerCast(subscript, expectedBlock, instructions);
      if (stripped == induction) {
        inductionIndices++;
      } else if (!(stripped instanceof Constant.Int)) {
        return null;
      }
    }
    return inductionIndices == 1 ? new AddressMatch(root, Set.copyOf(instructions)) : null;
  }

  private static Value stripIntegerCast(
      Value value, BasicBlock expectedBlock, Set<Instruction> instructions) {
    while (value instanceof Instruction instruction
        && (instruction.getOpcode() == Instruction.Opcode.SEXT
            || instruction.getOpcode() == Instruction.Opcode.ZEXT)) {
      if (instruction.getParent() != expectedBlock || instruction.getNumOperands() != 1) {
        return value;
      }
      instructions.add(instruction);
      value = instruction.getOperand(0);
    }
    return value;
  }

  private static boolean validLiveOuts(
      LoopAnalysis.Loop inner, Instruction reduction, Instruction outputStore) {
    for (BasicBlock block : inner.blocks()) {
      for (Instruction instruction : block.getInstructions()) {
        for (var use : instruction.getUses()) {
          if (inner.contains(use.getUser().getParent())) continue;
          if (instruction == reduction && use.getUser() == outputStore) continue;
          return false;
        }
      }
    }
    return true;
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
    List<InductionVariableAnalysis.Induction> matches = inductions.stream()
        .filter(induction -> induction.loop() == loop && induction.phi().getType() == Type.INT)
        .toList();
    return matches.size() == 1 ? matches.getFirst() : null;
  }

  private static Instruction compareFor(Instruction branch, Instruction induction) {
    if (branch != null
        && branch.getOpcode() == Instruction.Opcode.CONDBR
        && branch.getOperand(0) instanceof Instruction compare
        && compare.getOpcode() == Instruction.Opcode.ICMP
        && compare.getNumOperands() == 2
        && (compare.getOperand(0) == induction || compare.getOperand(1) == induction)) {
      return compare;
    }
    return null;
  }

  private static boolean canonicalHeader(BasicBlock header, int phiCount) {
    List<Instruction> instructions = header.getInstructions();
    if (instructions.size() != phiCount + 2) return false;
    for (int index = 0; index < phiCount; index++) {
      if (instructions.get(index).getOpcode() != Instruction.Opcode.PHI) return false;
    }
    return instructions.get(phiCount).getOpcode() == Instruction.Opcode.ICMP
        && instructions.get(phiCount + 1).getOpcode() == Instruction.Opcode.CONDBR;
  }

  private static List<Instruction> leadingPhis(BasicBlock block) {
    return block.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index + 1 < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }

  private static boolean containsIdentity(Instruction instruction, Value value) {
    return instruction.getOperand(0) == value || instruction.getOperand(1) == value;
  }

  private static boolean isI32Zero(Value value) {
    return value instanceof Constant.Int constant
        && constant.getType() == Type.INT
        && constant.value == 0;
  }

  private static boolean sameI32Value(Value left, Value right) {
    if (left == right) return true;
    return left instanceof Constant.Int leftConstant
        && right instanceof Constant.Int rightConstant
        && leftConstant.getType() == Type.INT
        && rightConstant.getType() == Type.INT
        && leftConstant.value == rightConstant.value;
  }

  private static boolean containsOpcode(BasicBlock block, Instruction.Opcode opcode) {
    return block.getInstructions().stream()
        .anyMatch(instruction -> instruction.getOpcode() == opcode);
  }

  private static boolean definedInside(Value value, LoopAnalysis.Loop loop) {
    return value instanceof Instruction instruction && loop.contains(instruction.getParent());
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
