package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.List;

/** Replaces side-effect-free affine counting loops with a guarded closed form. */
public final class AffineLoopSummarization {
  private AffineLoopSummarization() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (LoopAnalysis.Loop loop :
        new ArrayList<>(fam.getResult(LoopAnalysis.class, function).loops())) {
      Candidate candidate = Candidate.match(loop);
      if (candidate == null) continue;
      apply(function, candidate);
      changed = true;
    }
    return changed;
  }

  private static void apply(Function function, Candidate candidate) {
    BasicBlock header = candidate.loop.header();
    BasicBlock summary = function.insertBlockAfter(header, header.getLabel() + ".affine.summary");
    BasicBlock entry = summary;

    if (candidate.modulus != null) {
      BasicBlock nonnegative = function.insertBlockAfter(
          header, header.getLabel() + ".affine.nonnegative");
      BasicBlock range = function.insertBlockAfter(
          nonnegative, header.getLabel() + ".affine.range");
      entry = nonnegative;

      IRBuilder guardBuilder = new IRBuilder(nonnegative);
      Value nonnegativeStart = guardBuilder.createICmp(
          "sge", candidate.stateStart, Constant.intConst(0));
      guardBuilder.createCondBr(nonnegativeStart, range, candidate.body);

      IRBuilder rangeBuilder = new IRBuilder(range);
      Value room = rangeBuilder.createSub(
          Constant.intConst(Integer.MAX_VALUE), candidate.stateStart);
      Value maximumCount = rangeBuilder.createSDiv(room, candidate.stateStep);
      Value inRange = rangeBuilder.createICmp("sle", candidate.bound, maximumCount);
      rangeBuilder.createCondBr(inRange, summary, candidate.body);
    }

    IRBuilder summaryBuilder = new IRBuilder(summary);
    Value delta = summaryBuilder.createMul(candidate.bound, candidate.stateStep);
    Value finalState = summaryBuilder.createAdd(candidate.stateStart, delta);
    if (candidate.modulus != null) {
      finalState = summaryBuilder.createSRem(finalState, candidate.modulus);
    }
    summaryBuilder.createBr(header);

    Instruction branch = header.getTerminator();
    int bodyOperand = branch.getOperand(1) == candidate.body ? 1 : 2;
    branch.setOperand(bodyOperand, entry);
    candidate.induction.addOperand(candidate.bound);
    candidate.induction.addOperand(summary);
    candidate.state.addOperand(finalState);
    candidate.state.addOperand(summary);
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return AffineLoopSummarization.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }

  private record Candidate(
      LoopAnalysis.Loop loop,
      BasicBlock body,
      Instruction induction,
      Instruction state,
      Value bound,
      Value stateStart,
      Value stateStep,
      Value modulus) {

    private static Candidate match(LoopAnalysis.Loop loop) {
      if (loop.preheader() == null
          || loop.blocks().size() != 2
          || loop.latches().size() != 1) return null;
      BasicBlock header = loop.header();
      BasicBlock body = loop.latches().iterator().next();
      if (body == header || !loop.contains(body)) return null;

      Instruction branch = header.getTerminator();
      if (branch == null
          || branch.getOpcode() != Instruction.Opcode.CONDBR
          || !(branch.getOperand(0) instanceof Instruction compare)
          || compare.getOpcode() != Instruction.Opcode.ICMP
          || !"slt".equals(compare.getPredicate())) return null;
      boolean trueIsBody = branch.getOperand(1) == body;
      BasicBlock exit = trueIsBody
          ? (BasicBlock) branch.getOperand(2)
          : (BasicBlock) branch.getOperand(1);
      if (!trueIsBody || loop.contains(exit)) return null;

      List<Instruction> phis = header.getInstructions().stream()
          .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
          .toList();
      if (phis.size() != 2) return null;
      Instruction induction = null;
      Value bound = null;
      if (compare.getOperand(0) instanceof Instruction phi
          && phis.contains(phi)
          && isZeroStart(phi, loop.preheader())) {
        induction = phi;
        bound = compare.getOperand(1);
      }
      if (induction == null
          || induction.getType() != Type.INT
          || bound.getType() != Type.INT
          || definedInside(bound, loop)) return null;

      Instruction inductionNext = incomingInstruction(induction, body);
      if (!isAddOf(inductionNext, induction, 1)) return null;
      Instruction state = phis.get(0) == induction ? phis.get(1) : phis.get(0);
      if (state.getType() != Type.INT) return null;
      Value stateStart = incomingValue(state, loop.preheader());
      Instruction stateNext = incomingInstruction(state, body);
      if (stateStart == null || stateNext == null || definedInside(stateStart, loop)) return null;

      Value modulus = null;
      Instruction addition = stateNext;
      if (stateNext.getOpcode() == Instruction.Opcode.SREM) {
        if (!(stateNext.getOperand(1) instanceof Constant.Int constant)
            || constant.value <= 0
            || constant.value > Integer.MAX_VALUE
            || !(stateNext.getOperand(0) instanceof Instruction add)) return null;
        modulus = stateNext.getOperand(1);
        addition = add;
      }
      Value stateStep = invariantAddend(addition, state, loop);
      if (stateStep == null || stateStep.getType() != Type.INT) return null;
      if (modulus != null
          && (!(stateStep instanceof Constant.Int step)
              || step.value <= 0
              || step.value > Integer.MAX_VALUE)) return null;

      Instruction bodyBranch = body.getTerminator();
      if (bodyBranch == null
          || bodyBranch.getOpcode() != Instruction.Opcode.BR
          || bodyBranch.getOperand(0) != header) return null;
      for (Instruction instruction : body.getInstructions()) {
        if (instruction == bodyBranch
            || instruction == inductionNext
            || instruction == stateNext
            || instruction == addition) continue;
        return null;
      }
      if (hasEscapingUses(inductionNext, loop)
          || hasEscapingUses(stateNext, loop)
          || (addition != stateNext && hasUsesOtherThan(addition, stateNext))) return null;
      return new Candidate(
          loop, body, induction, state, bound, stateStart, stateStep, modulus);
    }

    private static boolean isZeroStart(Instruction phi, BasicBlock preheader) {
      Value start = incomingValue(phi, preheader);
      return start instanceof Constant.Int constant && constant.value == 0;
    }

    private static Value invariantAddend(
        Instruction addition, Instruction state, LoopAnalysis.Loop loop) {
      if (addition == null || addition.getOpcode() != Instruction.Opcode.ADD) return null;
      Value other;
      if (addition.getOperand(0) == state) other = addition.getOperand(1);
      else if (addition.getOperand(1) == state) other = addition.getOperand(0);
      else return null;
      return definedInside(other, loop) ? null : other;
    }

    private static boolean isAddOf(Instruction instruction, Value value, long constantValue) {
      if (instruction == null || instruction.getOpcode() != Instruction.Opcode.ADD) return false;
      return instruction.getOperand(0) == value
              && isConstant(instruction.getOperand(1), constantValue)
          || instruction.getOperand(1) == value
              && isConstant(instruction.getOperand(0), constantValue);
    }

    private static boolean isConstant(Value value, long expected) {
      return value instanceof Constant.Int constant && constant.value == expected;
    }

    private static Instruction incomingInstruction(Instruction phi, BasicBlock predecessor) {
      Value value = incomingValue(phi, predecessor);
      return value instanceof Instruction instruction ? instruction : null;
    }

    private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
      for (int index = 0; index < phi.getNumOperands(); index += 2) {
        if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
      }
      return null;
    }

    private static boolean definedInside(Value value, LoopAnalysis.Loop loop) {
      return value instanceof Instruction instruction && loop.contains(instruction.getParent());
    }

    private static boolean hasEscapingUses(Value value, LoopAnalysis.Loop loop) {
      return value.getUses().stream().anyMatch(use -> !loop.contains(use.getUser().getParent()));
    }

    private static boolean hasUsesOtherThan(Value value, Instruction allowedUser) {
      return value.getUses().stream().anyMatch(use -> use.getUser() != allowedUser);
    }
  }
}
