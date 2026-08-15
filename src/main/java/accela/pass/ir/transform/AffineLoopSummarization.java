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
import java.util.HashSet;
import java.util.List;
import java.util.Set;

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
    Value tripCount;

    boolean needsNonnegativeGuard = !isKnownNonnegative(candidate.inductionStart);
    boolean needsInductionRangeGuard = candidate.inductionStep > 1;
    if (needsNonnegativeGuard || needsInductionRangeGuard || candidate.hasRemainder()) {
      BasicBlock count = function.insertBlockAfter(header, header.getLabel() + ".affine.count");
      entry = count;
      IRBuilder builder = new IRBuilder(count);
      if (needsNonnegativeGuard) {
        Value nonnegative = builder.createICmp(
            "sge", candidate.induction, Constant.intConst(0));
        BasicBlock next = function.insertBlockAfter(count, header.getLabel() + ".affine.ivrange");
        builder.createCondBr(nonnegative, next, candidate.body);
        count = next;
        builder = new IRBuilder(count);
      }
      if (needsInductionRangeGuard) {
        long largestSafeBound = Integer.MAX_VALUE - (candidate.inductionStep - 1);
        Value inRange = builder.createICmp(
            "sle", candidate.bound, Constant.intConst(largestSafeBound));
        BasicBlock next = function.insertBlockAfter(count, header.getLabel() + ".affine.tripcount");
        builder.createCondBr(inRange, next, candidate.body);
        count = next;
        builder = new IRBuilder(count);
      }
      tripCount = createTripCount(builder, candidate);

      for (Recurrence recurrence : candidate.recurrences) {
        if (recurrence.modulus == null) continue;
        Value nonnegative = builder.createICmp(
            "sge", recurrence.phi, Constant.intConst(0));
        BasicBlock range = function.insertBlockAfter(count, header.getLabel() + ".affine.modrange");
        builder.createCondBr(nonnegative, range, candidate.body);

        builder = new IRBuilder(range);
        Value room = builder.createSub(Constant.intConst(Integer.MAX_VALUE), recurrence.phi);
        Value maximumCount = builder.createSDiv(room, recurrence.step);
        Value inRange = builder.createICmp("sle", tripCount, maximumCount);
        BasicBlock next = function.insertBlockAfter(range, header.getLabel() + ".affine.modnext");
        builder.createCondBr(inRange, next, candidate.body);
        count = next;
        builder = new IRBuilder(count);
      }
      builder.createBr(summary);
    } else {
      IRBuilder builder = new IRBuilder(summary);
      tripCount = createTripCount(builder, candidate);
    }

    IRBuilder summaryBuilder = new IRBuilder(summary);
    Value finalInduction = candidate.inductionStep == 1
        ? candidate.bound
        : summaryBuilder.createAdd(
            candidate.induction,
            summaryBuilder.createMul(tripCount, Constant.intConst(candidate.inductionStep)));
    List<Value> finalStates = new ArrayList<>();
    for (Recurrence recurrence : candidate.recurrences) {
      Value delta = summaryBuilder.createMul(tripCount, recurrence.step);
      Value finalState = summaryBuilder.createAdd(recurrence.phi, delta);
      if (recurrence.modulus != null) {
        finalState = summaryBuilder.createSRem(finalState, recurrence.modulus);
      }
      finalStates.add(finalState);
    }
    summaryBuilder.createBr(header);

    Instruction branch = header.getTerminator();
    int bodyOperand = branch.getOperand(1) == candidate.body ? 1 : 2;
    branch.setOperand(bodyOperand, entry);
    candidate.induction.addOperand(finalInduction);
    candidate.induction.addOperand(summary);
    for (int index = 0; index < candidate.recurrences.size(); index++) {
      candidate.recurrences.get(index).phi.addOperand(finalStates.get(index));
      candidate.recurrences.get(index).phi.addOperand(summary);
    }
  }

  private static Value createTripCount(IRBuilder builder, Candidate candidate) {
    Value difference = builder.createSub(candidate.bound, candidate.induction);
    if (candidate.inductionStep == 1) return difference;
    Value adjusted = builder.createSub(difference, Constant.intConst(1));
    Value quotient = builder.createSDiv(adjusted, Constant.intConst(candidate.inductionStep));
    return builder.createAdd(quotient, Constant.intConst(1));
  }

  private static boolean isKnownNonnegative(Value value) {
    return value instanceof Constant.Int constant && constant.value >= 0;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return AffineLoopSummarization.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }

  private record Recurrence(
      Instruction phi,
      Value step,
      Instruction update,
      Instruction addition,
      Value modulus) {}

  private record Candidate(
      LoopAnalysis.Loop loop,
      BasicBlock body,
      Instruction induction,
      Value inductionStart,
      long inductionStep,
      Value bound,
      List<Recurrence> recurrences) {

    private boolean hasRemainder() {
      return recurrences.stream().anyMatch(recurrence -> recurrence.modulus != null);
    }

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
          || compare.getOpcode() != Instruction.Opcode.ICMP) return null;
      boolean trueInside = loop.contains((BasicBlock) branch.getOperand(1));
      boolean falseInside = loop.contains((BasicBlock) branch.getOperand(2));
      if (trueInside == falseInside) return null;
      BasicBlock inside = (BasicBlock) branch.getOperand(trueInside ? 1 : 2);
      if (inside != body) return null;

      List<Instruction> phis = header.getInstructions().stream()
          .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
          .toList();
      String predicate = trueInside ? compare.getPredicate() : invert(compare.getPredicate());
      if (predicate == null) return null;
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
          || definedInside(bound, loop)) return null;

      Value inductionStart = incomingValue(induction, loop.preheader());
      Instruction inductionNext = incomingInstruction(induction, body);
      Long inductionStep = positiveConstantStep(inductionNext, induction);
      if (inductionStart == null || inductionStep == null) return null;

      List<Recurrence> recurrences = new ArrayList<>();
      for (Instruction phi : phis) {
        if (phi == induction) continue;
        Recurrence recurrence = matchRecurrence(phi, loop, body);
        if (recurrence == null) return null;
        recurrences.add(recurrence);
      }
      if (recurrences.isEmpty()) return null;

      Instruction bodyBranch = body.getTerminator();
      if (bodyBranch == null
          || bodyBranch.getOpcode() != Instruction.Opcode.BR
          || bodyBranch.getOperand(0) != header) return null;
      Set<Instruction> allowed = new HashSet<>();
      allowed.add(bodyBranch);
      allowed.add(inductionNext);
      for (Recurrence recurrence : recurrences) {
        allowed.add(recurrence.update);
        allowed.add(recurrence.addition);
      }
      if (body.getInstructions().stream().anyMatch(instruction -> !allowed.contains(instruction))) {
        return null;
      }
      if (hasEscapingUses(inductionNext, loop)) return null;
      for (Recurrence recurrence : recurrences) {
        if (hasEscapingUses(recurrence.update, loop)
            || recurrence.addition != recurrence.update
                && hasUsesOtherThan(recurrence.addition, recurrence.update)) return null;
      }
      return new Candidate(
          loop, body, induction, inductionStart,
          inductionStep, bound, List.copyOf(recurrences));
    }

    private static Recurrence matchRecurrence(
        Instruction phi, LoopAnalysis.Loop loop, BasicBlock body) {
      if (!isIntegerState(phi.getType())) return null;
      Value start = incomingValue(phi, loop.preheader());
      Instruction update = incomingInstruction(phi, body);
      if (start == null || update == null || definedInside(start, loop)) return null;

      Value modulus = null;
      Instruction addition = update;
      if (update.getOpcode() == Instruction.Opcode.SREM) {
        if (phi.getType() != Type.INT) return null;
        if (!(update.getOperand(1) instanceof Constant.Int constant)
            || constant.value <= 0
            || constant.value > Integer.MAX_VALUE
            || !(update.getOperand(0) instanceof Instruction add)) return null;
        modulus = update.getOperand(1);
        addition = add;
      }
      Value step = invariantAddend(addition, phi, loop);
      if (step == null || !step.getType().equals(phi.getType())) return null;
      if (modulus != null
          && (!(step instanceof Constant.Int constant)
              || constant.value <= 0
              || constant.value > Integer.MAX_VALUE)) return null;
      return new Recurrence(phi, step, update, addition, modulus);
    }

    private static boolean isIntegerState(Type type) {
      return type == Type.INT
          || type.isVector() && type.getElementType() == Type.INT;
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

    private static Long positiveConstantStep(Instruction instruction, Value value) {
      if (instruction == null || instruction.getOpcode() != Instruction.Opcode.ADD) return null;
      Value other;
      if (instruction.getOperand(0) == value) other = instruction.getOperand(1);
      else if (instruction.getOperand(1) == value) other = instruction.getOperand(0);
      else return null;
      if (!(other instanceof Constant.Int constant)
          || constant.value <= 0
          || constant.value > Integer.MAX_VALUE) return null;
      return constant.value;
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
