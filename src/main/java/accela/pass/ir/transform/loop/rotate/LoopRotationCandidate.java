package accela.pass.ir.transform.loop.rotate;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import java.util.List;

/** The restricted canonical shape supported by the local loop-rotation utility. */
record LoopRotationCandidate(
    BasicBlock header,
    BasicBlock preheader,
    Instruction branch,
    BasicBlock body,
    BasicBlock exit,
    BasicBlock latch,
    Instruction latchBranch,
    List<Instruction> phis,
    List<Instruction> tests) {

  static LoopRotationCandidate match(LoopAnalysis.Loop loop) {
    return match(loop, false);
  }

  static LoopRotationCandidate matchForPointerExit(LoopAnalysis.Loop loop) {
    return match(loop, true);
  }

  private static LoopRotationCandidate match(
      LoopAnalysis.Loop loop, boolean allowLiveOuts) {
    BasicBlock header = loop.header();
    BasicBlock preheader =
        allowLiveOuts ? uniqueOutsidePredecessor(loop) : loop.preheader();
    Instruction branch = header.getTerminator();
    if (preheader == null || loop.latches().size() != 1
        || branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR) return null;

    BasicBlock trueTarget = (BasicBlock) branch.getOperand(1);
    BasicBlock falseTarget = (BasicBlock) branch.getOperand(2);
    if (loop.contains(trueTarget) == loop.contains(falseTarget)) return null;
    BasicBlock body = loop.contains(trueTarget) ? trueTarget : falseTarget;
    BasicBlock exit = body == trueTarget ? falseTarget : trueTarget;
    BasicBlock latch = loop.latches().iterator().next();
    Instruction latchBranch = latch.getTerminator();
    if (body.getPredecessors().size() != 1 || body.getPredecessors().getFirst() != header
        || latchBranch == null || latchBranch.getOpcode() != Instruction.Opcode.BR
        || latchBranch.getOperand(0) != header) return null;

    List<Instruction> instructions = header.getInstructions();
    List<Instruction> phis = instructions.stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI).toList();
    List<Instruction> tests = instructions.subList(phis.size(), instructions.size() - 1);
    if (tests.size() > 16 || tests.stream().anyMatch(test -> !isDuplicable(test))
        || phis.stream().anyMatch(phi -> hasOutsideUse(phi, loop, header))
            && (!allowLiveOuts || exit.getPredecessors().size() != 1
                || exit.getPredecessors().getFirst() != header)
        || tests.stream().anyMatch(test -> hasNonHeaderUse(test, header))) return null;
    return new LoopRotationCandidate(
        header, preheader, branch, body, exit, latch, latchBranch, phis, tests);
  }

  private static BasicBlock uniqueOutsidePredecessor(LoopAnalysis.Loop loop) {
    List<BasicBlock> predecessors = loop.header().getPredecessors().stream()
        .filter(block -> !loop.contains(block))
        .toList();
    return predecessors.size() == 1 ? predecessors.getFirst() : null;
  }

  boolean bodyOnTrueEdge() {
    return branch.getOperand(1) == body;
  }

  /** Whether the header computes loop-varying division or remainder. */
  boolean hasVariantDivisionOrRemainderHeaderWork(LoopAnalysis.Loop loop) {
    return tests.stream().anyMatch(instruction ->
        isDivision(instruction)
            && hasLoopVariantOperand(instruction, loop));
  }

  private static boolean hasLoopVariantOperand(
      Instruction instruction, LoopAnalysis.Loop loop) {
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      if (instruction.getOperand(index) instanceof Instruction definition
          && loop.contains(definition.getParent())) return true;
    }
    return false;
  }

  private static boolean isDivision(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case SDIV, SREM, FDIV -> true;
      default -> false;
    };
  }

  boolean exposesInvariantPureCall(
      LoopAnalysis.Loop loop, GlobalModRefAnalysis.Result modRef) {
    return body.getInstructions().stream().anyMatch(instruction ->
        instruction.getOpcode() == Instruction.Opcode.CALL
            && instruction.hasUses()
            && modRef.isPure(instruction)
            && operandsAreInvariant(instruction, loop));
  }

  private static boolean operandsAreInvariant(
      Instruction instruction, LoopAnalysis.Loop loop) {
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      Value operand = instruction.getOperand(index);
      if (operand instanceof Instruction definition
          && loop.contains(definition.getParent())) return false;
    }
    return true;
  }

  private static boolean isDuplicable(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR,
          FADD, FSUB, FMUL, FDIV, FNEG, ICMP, FCMP, GEP, ZEXT, SEXT, SITOFP, FPTOSI, LOAD,
          BUILD_VECTOR, SPLAT, EXTRACT_ELEMENT, INSERT_ELEMENT, SHUFFLE_VECTOR, SELECT -> true;
      default -> false;
    };
  }

  private static boolean hasOutsideUse(
      Instruction instruction, LoopAnalysis.Loop loop, BasicBlock header) {
    return instruction.getUses().stream().anyMatch(
        use -> use.getUser().getParent() != header && !loop.contains(use.getUser().getParent()));
  }

  private static boolean hasNonHeaderUse(Instruction instruction, BasicBlock header) {
    return instruction.getUses().stream().anyMatch(use -> use.getUser().getParent() != header);
  }
}
