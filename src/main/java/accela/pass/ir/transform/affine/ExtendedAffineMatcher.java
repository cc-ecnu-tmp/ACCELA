package accela.pass.ir.transform.affine;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.analysis.scev.SCEV;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/** Matches exact i32 polynomial reductions in canonical scalar loops. */
public final class ExtendedAffineMatcher {
  public enum Failure {
    CANONICAL_LOOP,
    SCEV_AFFINE_STATE,
    EXACT_TRIP_COUNT,
    ZERO_NEGATIVE_ITERATIONS,
    MODULO_I32_EQUIVALENCE,
    SIDE_EFFECT_FREE_BODY,
    LIVE_OUTS
  }

  public record Inspection(Plan plan, Failure failure) {
    public Inspection {
      if ((plan == null) == (failure == null)) {
        throw new IllegalArgumentException("inspection requires exactly one plan or failure");
      }
    }

    public boolean matched() {
      return plan != null;
    }
  }

  public record StateRecurrence(
      Instruction phi,
      Instruction update,
      SCEV.IterationPolynomial delta) {
    public StateRecurrence {
      Objects.requireNonNull(phi, "phi");
      Objects.requireNonNull(update, "update");
      Objects.requireNonNull(delta, "delta");
    }
  }

  public record Plan(
      LoopAnalysis.Loop loop,
      BasicBlock body,
      Instruction headerBranch,
      int insideSuccessorOperand,
      Instruction induction,
      Instruction inductionUpdate,
      int inductionStart,
      int inductionStep,
      Value bound,
      List<StateRecurrence> recurrences,
      int bodyArithmeticInstructions) {
    public Plan {
      Objects.requireNonNull(loop, "loop");
      Objects.requireNonNull(body, "body");
      Objects.requireNonNull(headerBranch, "headerBranch");
      Objects.requireNonNull(induction, "induction");
      Objects.requireNonNull(inductionUpdate, "inductionUpdate");
      Objects.requireNonNull(bound, "bound");
      recurrences = List.copyOf(recurrences);
      if (insideSuccessorOperand != 1 && insideSuccessorOperand != 2) {
        throw new IllegalArgumentException("invalid inside successor operand");
      }
      if (inductionStart < 0 || inductionStep < 1 || recurrences.isEmpty()) {
        throw new IllegalArgumentException("invalid extended affine plan");
      }
    }

    public int maximumDeltaDegree() {
      return recurrences.stream().mapToInt(state -> state.delta().degree()).max().orElse(0);
    }
  }

  private ExtendedAffineMatcher() {}

  public static Inspection inspect(
      LoopAnalysis.Loop loop, ScalarEvolutionAnalysis.Result scalarEvolution) {
    Objects.requireNonNull(loop, "loop");
    Objects.requireNonNull(scalarEvolution, "scalarEvolution");

    if (loop.preheader() == null
        || loop.blocks().size() != 2
        || loop.latches().size() != 1) return rejected(Failure.CANONICAL_LOOP);
    BasicBlock header = loop.header();
    BasicBlock body = loop.latches().iterator().next();
    if (body == header || !loop.contains(body)) return rejected(Failure.CANONICAL_LOOP);

    Instruction bodyBranch = body.getTerminator();
    if (bodyBranch == null
        || bodyBranch.getOpcode() != Instruction.Opcode.BR
        || bodyBranch.getOperand(0) != header) return rejected(Failure.CANONICAL_LOOP);
    Instruction headerBranch = header.getTerminator();
    if (headerBranch == null
        || headerBranch.getOpcode() != Instruction.Opcode.CONDBR
        || !(headerBranch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) {
      return rejected(Failure.CANONICAL_LOOP);
    }

    List<Instruction> phis = header.getInstructions().stream()
        .takeWhile(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .toList();
    if (phis.isEmpty()
        || header.getInstructions().size() != phis.size() + 2
        || header.getInstructions().get(phis.size()) != compare) {
      return rejected(Failure.CANONICAL_LOOP);
    }
    boolean trueInside = loop.contains((BasicBlock) headerBranch.getOperand(1));
    boolean falseInside = loop.contains((BasicBlock) headerBranch.getOperand(2));
    if (trueInside == falseInside) return rejected(Failure.CANONICAL_LOOP);
    int insideOperand = trueInside ? 1 : 2;
    if (headerBranch.getOperand(insideOperand) != body) return rejected(Failure.CANONICAL_LOOP);

    String predicate = trueInside ? compare.getPredicate() : invert(compare.getPredicate());
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
    if (induction == null || !"slt".equals(predicate)
        || induction.getType() != Type.INT || bound.getType() != Type.INT) {
      return rejected(Failure.CANONICAL_LOOP);
    }
    SCEV boundExpression = scalarEvolution.getSCEV(bound);
    if (!scalarEvolution.isLoopInvariant(boundExpression, loop)) {
      return rejected(Failure.EXACT_TRIP_COUNT);
    }

    SCEV inductionExpression = scalarEvolution.getSCEV(induction);
    if (!(inductionExpression instanceof SCEV.AddRec inductionRecurrence)
        || inductionRecurrence.loop() != loop
        || !(inductionRecurrence.start() instanceof SCEV.Constant start)
        || !(inductionRecurrence.step() instanceof SCEV.Constant step)) {
      return rejected(Failure.EXACT_TRIP_COUNT);
    }
    BigInteger startValue = start.value();
    BigInteger stepValue = step.value();
    if (stepValue.signum() <= 0
        || stepValue.compareTo(BigInteger.valueOf(Integer.MAX_VALUE)) > 0) {
      return rejected(Failure.EXACT_TRIP_COUNT);
    }
    if (startValue.signum() < 0) return rejected(Failure.ZERO_NEGATIVE_ITERATIONS);
    if (startValue.compareTo(BigInteger.valueOf(Integer.MAX_VALUE)) > 0) {
      return rejected(Failure.EXACT_TRIP_COUNT);
    }
    Instruction inductionUpdate = incomingInstruction(induction, body);
    if (inductionUpdate == null) return rejected(Failure.EXACT_TRIP_COUNT);

    for (Instruction instruction : body.getInstructions()) {
      if (instruction == bodyBranch) continue;
      switch (instruction.getOpcode()) {
        case ADD, SUB, MUL -> { }
        case SDIV, SREM, SMULH, SHL, ASHR, AND, XOR,
            ZEXT, SEXT, SITOFP, FPTOSI,
            FADD, FSUB, FMUL, FDIV, FNEG -> {
          return rejected(Failure.MODULO_I32_EQUIVALENCE);
        }
        case LOAD, STORE, CALL, ALLOCA, GEP -> {
          return rejected(Failure.SIDE_EFFECT_FREE_BODY);
        }
        default -> {
          return rejected(Failure.CANONICAL_LOOP);
        }
      }
    }

    List<StateRecurrence> recurrences = new ArrayList<>();
    for (Instruction phi : phis) {
      if (phi == induction) continue;
      if (phi.getType() != Type.INT) return rejected(Failure.MODULO_I32_EQUIVALENCE);
      Value initial = incomingValue(phi, loop.preheader());
      Instruction update = incomingInstruction(phi, body);
      if (initial == null || update == null
          || !scalarEvolution.isLoopInvariant(scalarEvolution.getSCEV(initial), loop)) {
        return rejected(Failure.SCEV_AFFINE_STATE);
      }
      SCEV delta = scalarEvolution.getAdditiveRecurrenceDelta(phi, update, loop).orElse(null);
      if (delta == null) return rejected(Failure.SCEV_AFFINE_STATE);
      SCEV.IterationPolynomial polynomial =
          scalarEvolution.getIntegerIterationPolynomial(delta, loop).orElse(null);
      if (polynomial == null) return rejected(Failure.MODULO_I32_EQUIVALENCE);
      recurrences.add(new StateRecurrence(phi, update, polynomial));
    }
    if (recurrences.isEmpty()) return rejected(Failure.SCEV_AFFINE_STATE);

    if (hasEscapingUses(inductionUpdate, loop)
        || body.getInstructions().stream()
            .filter(instruction -> instruction != bodyBranch)
            .anyMatch(instruction -> hasEscapingUses(instruction, loop))) {
      return rejected(Failure.LIVE_OUTS);
    }
    boolean hasLiveState = recurrences.stream().map(StateRecurrence::phi)
        .anyMatch(phi -> phi.getUses().stream()
            .map(Use::getUser).anyMatch(user -> !loop.contains(user.getParent())));
    if (!hasLiveState) return rejected(Failure.LIVE_OUTS);

    int arithmeticInstructions = Math.toIntExact(body.getInstructions().stream()
        .filter(instruction -> instruction != bodyBranch).count());
    return new Inspection(new Plan(
        loop, body, headerBranch, insideOperand, induction, inductionUpdate,
        startValue.intValueExact(), stepValue.intValueExact(), bound,
        recurrences, arithmeticInstructions), null);
  }

  private static Inspection rejected(Failure failure) {
    return new Inspection(null, failure);
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    if (phi.getNumOperands() % 2 != 0) return null;
    Value result = null;
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) != predecessor) continue;
      if (result != null) return null;
      result = phi.getOperand(index);
    }
    return result;
  }

  private static Instruction incomingInstruction(Instruction phi, BasicBlock predecessor) {
    Value value = incomingValue(phi, predecessor);
    return value instanceof Instruction instruction && instruction.getParent() == predecessor
        ? instruction : null;
  }

  private static boolean hasEscapingUses(Value value, LoopAnalysis.Loop loop) {
    return value.getUses().stream().anyMatch(use -> !loop.contains(use.getUser().getParent()));
  }

  private static String invert(String predicate) {
    if (predicate == null) return null;
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
    if (predicate == null) return null;
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
