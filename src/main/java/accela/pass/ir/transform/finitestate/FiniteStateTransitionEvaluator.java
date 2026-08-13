package accela.pass.ir.transform.finitestate;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.ExactI32;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.IdentityHashMap;
import java.util.Map;

/** Executes one proven-pure transition CFG with exact i32 operations. */
final class FiniteStateTransitionEvaluator {
  static final class EvaluationFailure extends Exception {
    EvaluationFailure(String message) {
      super(message);
    }

    EvaluationFailure(String message, Throwable cause) {
      super(message, cause);
    }
  }

  private final LoopAnalysis.Loop loop;
  private final BasicBlock bodyEntry;
  private final BasicBlock header;
  private final Instruction induction;
  private final Instruction state;
  private final Value nextState;

  FiniteStateTransitionEvaluator(
      LoopAnalysis.Loop loop,
      BasicBlock bodyEntry,
      Instruction induction,
      Instruction state,
      Value nextState) {
    this.loop = loop;
    this.bodyEntry = bodyEntry;
    this.header = loop.header();
    this.induction = induction;
    this.state = state;
    this.nextState = nextState;
  }

  int evaluate(int initialState) throws EvaluationFailure {
    Map<Value, Integer> values = new IdentityHashMap<>();
    values.put(state, initialState);
    // The matcher proves transition decisions are independent of the induction. Supplying zero
    // only evaluates the unrelated canonical increment; it does not weaken that dependency proof.
    values.put(induction, 0);
    BasicBlock predecessor = header;
    BasicBlock block = bodyEntry;

    int blockLimit = loop.blocks().size() + 1;
    for (int visited = 0; visited < blockLimit; visited++) {
      evaluatePhis(block, predecessor, values);
      BasicBlock successor = null;
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.PHI) continue;
        if (instruction.isTerminator()) {
          successor = successor(instruction, values);
          break;
        }
        values.put(instruction, evaluateInstruction(instruction, values));
      }
      if (successor == null) {
        throw new EvaluationFailure("transition block has no supported terminator");
      }
      if (successor == header) return value(nextState, values);
      if (!loop.contains(successor)) {
        throw new EvaluationFailure("transition exits before its canonical latch");
      }
      predecessor = block;
      block = successor;
    }
    throw new EvaluationFailure("transition CFG is cyclic or exceeds its proven block bound");
  }

  private static void evaluatePhis(
      BasicBlock block, BasicBlock predecessor, Map<Value, Integer> values)
      throws EvaluationFailure {
    Map<Instruction, Integer> incomingValues = new IdentityHashMap<>();
    for (Instruction instruction : block.getInstructions()) {
      if (instruction.getOpcode() != Instruction.Opcode.PHI) break;
      Value incoming = incomingValue(instruction, predecessor);
      if (incoming == null) {
        throw new EvaluationFailure(
            "transition phi has no incoming value for predecessor " + predecessor.getLabel());
      }
      incomingValues.put(instruction, value(incoming, values));
    }
    values.putAll(incomingValues);
  }

  private static BasicBlock successor(Instruction terminator, Map<Value, Integer> values)
      throws EvaluationFailure {
    return switch (terminator.getOpcode()) {
      case BR -> (BasicBlock) terminator.getOperand(0);
      case CONDBR -> (BasicBlock) terminator.getOperand(
          value(terminator.getOperand(0), values) != 0 ? 1 : 2);
      default -> throw new EvaluationFailure(
          "unsupported transition terminator " + terminator.getOpcode());
    };
  }

  private static int evaluateInstruction(
      Instruction instruction, Map<Value, Integer> values) throws EvaluationFailure {
    try {
      return switch (instruction.getOpcode()) {
        case ADD -> ExactI32.add(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case SUB -> ExactI32.subtract(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case MUL -> ExactI32.multiply(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case SMULH -> ExactI32.multiplyHigh(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case SDIV -> ExactI32.divide(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case SREM -> ExactI32.remainder(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case SHL -> ExactI32.shiftLeft(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case ASHR -> ExactI32.arithmeticShiftRight(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case AND -> ExactI32.and(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case XOR -> ExactI32.xor(
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values));
        case ICMP -> ExactI32.compare(
            instruction.getPredicate(),
            value(instruction.getOperand(0), values),
            value(instruction.getOperand(1), values)) ? 1 : 0;
        default -> throw new EvaluationFailure(
            "unsupported exact-i32 transition instruction " + instruction.getOpcode());
      };
    } catch (ArithmeticException | IllegalArgumentException exception) {
      throw new EvaluationFailure(
          "transition instruction has undefined or unsupported i32 semantics", exception);
    }
  }

  private static int value(Value value, Map<Value, Integer> values) throws EvaluationFailure {
    if (value instanceof Constant.Int integer) {
      if (integer.getType() != Type.INT && integer.getType() != Type.I1) {
        throw new EvaluationFailure("transition uses a non-i32 integer constant");
      }
      int normalized = ExactI32.normalize(integer.value);
      return integer.getType() == Type.I1 ? (normalized == 0 ? 0 : 1) : normalized;
    }
    Integer result = values.get(value);
    if (result == null) {
      throw new EvaluationFailure("transition depends on an unavailable runtime value");
    }
    return result;
  }

  private static Value incomingValue(Instruction phi, BasicBlock predecessor) {
    for (int index = 0; index + 1 < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index + 1) == predecessor) return phi.getOperand(index);
    }
    return null;
  }
}
