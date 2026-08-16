package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

public final class InstSimplify {
  private InstSimplify() {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (!runOnFunction(function)) {
        return PreservedAnalyses.all();
      }
      return PreservedAnalyses.none();
    }
  }

  public static boolean runOnFunction(Function function) {
    Set<Instruction> worklist = new LinkedHashSet<>();
    for (BasicBlock bb : function.getBlocks()) {
      worklist.addAll(bb.getInstructions());
    }

    boolean changed = false;
    int fuel = 10000;

    while (!worklist.isEmpty() && fuel-- > 0) {
      Instruction inst = worklist.iterator().next();
      worklist.remove(inst);
      if (inst.getParent() == null) continue;

      Value result = trySimplify(inst);
      if (result != null) {
        for (var use : new ArrayList<>(inst.getUses())) {
          if (use.getUser() instanceof Instruction user) {
            worklist.add(user);
          }
        }
        inst.replaceAllUsesWith(result);
        inst.eraseFromParent();
        changed = true;
        continue;
      }

      if (tryDeadCode(inst)) {
        changed = true;
        continue;
      }

      if (tryConstantBranch(inst, worklist)) {
        changed = true;
      }
    }
    return changed;
  }

  private static Value trySimplify(Instruction inst) {
    Value v = tryConstantFold(inst);
    if (v != null) return v;
    return tryAlgebraicSimplify(inst);
  }

  private static Value tryConstantFold(Instruction inst) {
    switch (inst.getOpcode()) {
      case ADD: case SUB: case MUL: case SMULH: case SDIV: case SREM:
      case SHL: case ASHR: case AND: case XOR:
        return foldIntegerBinary(inst);
      case FADD: case FSUB: case FMUL: case FDIV:
        return foldFloatBinary(inst);
      case FNEG:
        return foldFloatNegation(inst);
      case ICMP: {
        return foldIntegerCompare(inst);
      }
      case FCMP:
        return foldFloatCompare(inst);
      case ZEXT: case SEXT: case SITOFP: case FPTOSI:
        return foldConversion(inst);
      case BUILD_VECTOR:
        return foldBuildVector(inst);
      case SPLAT:
        return foldSplat(inst);
      case EXTRACT_ELEMENT:
        return foldExtractElement(inst);
      case INSERT_ELEMENT:
        return foldInsertElement(inst);
      case SHUFFLE_VECTOR:
        return foldShuffleVector(inst);
      default: return null;
    }
  }

  private static Value tryAlgebraicSimplify(Instruction inst) {
    switch (inst.getOpcode()) {
      case ADD: {
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        if (isZero(inst.getOperand(0))) return inst.getOperand(1);
        return null;
      }
      case SUB: {
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        if (inst.getOperand(0) == inst.getOperand(1)) return zeroOf(inst.getType());
        return null;
      }
      case MUL: {
        if (isOne(inst.getOperand(1))) return inst.getOperand(0);
        if (isOne(inst.getOperand(0))) return inst.getOperand(1);
        if (isZero(inst.getOperand(0)) || isZero(inst.getOperand(1)))
          return zeroOf(inst.getType());
        return null;
      }
      case SDIV: {
        if (isOne(inst.getOperand(1))) return inst.getOperand(0);
        return null;
      }
      case SREM: {
        if (isOne(inst.getOperand(1))) return zeroOf(inst.getType());
        return null;
      }
      case SHL: case ASHR: {
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        if (isZero(inst.getOperand(0))) return zeroOf(inst.getType());
        return null;
      }
      case AND: {
        if (inst.getOperand(0) == inst.getOperand(1)) return inst.getOperand(0);
        if (isZero(inst.getOperand(0)) || isZero(inst.getOperand(1)))
          return zeroOf(inst.getType());
        if (isAllOnes(inst.getOperand(0))) return inst.getOperand(1);
        if (isAllOnes(inst.getOperand(1))) return inst.getOperand(0);
        return null;
      }
      case XOR: {
        if (inst.getOperand(0) == inst.getOperand(1)) return zeroOf(inst.getType());
        if (isZero(inst.getOperand(0))) return inst.getOperand(1);
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        return null;
      }
      case ICMP: {
        Value selfComparison = simplifySelfCompare(inst);
        return selfComparison != null ? selfComparison : simplifyBooleanCompare(inst);
      }
      case FNEG:
        if (inst.getOperand(0) instanceof Instruction negation
            && negation.getOpcode() == Opcode.FNEG) return negation.getOperand(0);
        return null;
      case PHI:
        return simplifyPhi(inst);
      case SELECT:
        if (inst.getOperand(0) instanceof Constant.Int condition)
          return inst.getOperand(condition.value != 0 ? 1 : 2);
        if (inst.getOperand(1) == inst.getOperand(2)) return inst.getOperand(1);
        return null;
      default: return null;
    }
  }

  private static Value simplifyBooleanCompare(Instruction compare) {
    if (!compare.getPredicate().equals("ne")) return null;
    Value value = isZero(compare.getOperand(1))
        ? compare.getOperand(0)
        : isZero(compare.getOperand(0)) ? compare.getOperand(1) : null;
    if (!(value instanceof Instruction extension)
        || extension.getOpcode() != Opcode.ZEXT
        || scalarType(extension.getOperand(0).getType()) != Type.I1
        || !extension.getOperand(0).getType().equals(compare.getType())) return null;
    // zext preserves the only two i1 values, so zext(c) != 0 is exactly c.
    return extension.getOperand(0);
  }

  private static Value simplifySelfCompare(Instruction compare) {
    if (compare.getOperand(0) != compare.getOperand(1)) return null;
    return switch (compare.getPredicate()) {
      case "eq", "sle", "sge" -> booleanOf(compare.getType(), true);
      case "ne", "slt", "sgt" -> booleanOf(compare.getType(), false);
      default -> null;
    };
  }

  private static Value simplifyPhi(Instruction phi) {
    if (phi.getNumOperands() == 0) return null;
    Value incoming = phi.getOperand(0);
    if (incoming == phi) return null;
    for (int index = 2; index < phi.getNumOperands(); index += 2) {
      if (phi.getOperand(index) != incoming) return null;
    }
    return incoming;
  }

  private static boolean tryDeadCode(Instruction inst) {
    if (inst.isTerminator()) return false;
    if (!inst.hasResult()) return false;
    if (inst.hasUses()) return false;
    if (hasSideEffect(inst)) return false;
    inst.eraseFromParent();
    return true;
  }

  private static boolean tryConstantBranch(Instruction inst, Set<Instruction> worklist) {
    if (inst.getOpcode() != Opcode.CONDBR) return false;
    if (!(inst.getOperand(0) instanceof Constant.Int ci)) return false;
    BasicBlock target = ci.value != 0
        ? (BasicBlock) inst.getOperand(1)
        : (BasicBlock) inst.getOperand(2);
    BasicBlock bb = inst.getParent();
    CFGUpdateUtils.rewriteCondBrToBr(bb, target);
    for (Instruction i : target.getInstructions()) {
      worklist.add(i);
    }
    return true;
  }

  private static boolean hasSideEffect(Instruction inst) {
    switch (inst.getOpcode()) {
      case STORE: case CALL: case RET: case BR: case CONDBR:
        return true;
      default:
        return false;
    }
  }

  private static boolean isZero(Value value) {
    if (value instanceof Constant.Zero) return true;
    if (value instanceof Constant.Int integer) return integer.value == 0;
    if (value instanceof Constant.Float floating)
      return Float.floatToRawIntBits(floating.value) == 0;
    return value instanceof Constant.Vector vector
        && vector.elements.stream().allMatch(InstSimplify::isZero);
  }

  private static boolean isOne(Value value) {
    if (value instanceof Constant.Int integer) return integer.value == 1;
    return value instanceof Constant.Vector vector
        && vector.elements.stream().allMatch(InstSimplify::isOne);
  }

  private static boolean isAllOnes(Value value) {
    if (value instanceof Constant.Int integer) {
      Type type = integer.getType();
      return type == Type.I1 ? integer.value != 0
          : type == Type.INT ? (int) integer.value == -1 : integer.value == -1;
    }
    return value instanceof Constant.Vector vector
        && vector.elements.stream().allMatch(InstSimplify::isAllOnes);
  }

  private static Constant zeroOf(Type type) {
    if (type.isVector() || type == Type.FLOAT) return Constant.zero(type);
    if (type == Type.I1) return Constant.boolConst(false);
    if (type == Type.I64) return Constant.int64Const(0);
    return Constant.intConst(0);
  }

  private static Constant booleanOf(Type type, boolean value) {
    if (!type.isVector()) return Constant.boolConst(value);
    return Constant.vector(
        type, Collections.nCopies(type.getLaneCount(), Constant.boolConst(value)));
  }

  private static Value foldIntegerBinary(Instruction inst) {
    List<Constant> left = constantElements(inst.getOperand(0));
    List<Constant> right = constantElements(inst.getOperand(1));
    if (left == null || right == null || left.size() != right.size()) return null;
    Type elementType = scalarType(inst.getType());
    List<Constant> result = new ArrayList<>();
    for (int lane = 0; lane < left.size(); lane++) {
      Long lhs = integerValue(left.get(lane));
      Long rhs = integerValue(right.get(lane));
      if (lhs == null || rhs == null) return null;
      Long folded = evalInt(inst.getOpcode(), elementType, lhs, rhs);
      if (folded == null) return null;
      result.add(integerConstant(elementType, folded));
    }
    return packConstant(inst.getType(), result);
  }

  private static Value foldFloatBinary(Instruction inst) {
    List<Constant> left = constantElements(inst.getOperand(0));
    List<Constant> right = constantElements(inst.getOperand(1));
    if (left == null || right == null || left.size() != right.size()) return null;
    List<Constant> result = new ArrayList<>();
    for (int lane = 0; lane < left.size(); lane++) {
      java.lang.Float lhs = floatingValue(left.get(lane));
      java.lang.Float rhs = floatingValue(right.get(lane));
      if (lhs == null || rhs == null) return null;
      float folded = switch (inst.getOpcode()) {
        case FADD -> lhs + rhs;
        case FSUB -> lhs - rhs;
        case FMUL -> lhs * rhs;
        case FDIV -> lhs / rhs;
        default -> throw new IllegalStateException("not a floating binary instruction");
      };
      result.add(Constant.floatConst(folded));
    }
    return packConstant(inst.getType(), result);
  }

  private static Value foldFloatNegation(Instruction inst) {
    List<Constant> operands = constantElements(inst.getOperand(0));
    if (operands == null) return null;
    List<Constant> result = new ArrayList<>();
    for (Constant operand : operands) {
      java.lang.Float value = floatingValue(operand);
      if (value == null) return null;
      result.add(Constant.floatConst(-value));
    }
    return packConstant(inst.getType(), result);
  }

  private static Value foldIntegerCompare(Instruction inst) {
    List<Constant> left = constantElements(inst.getOperand(0));
    List<Constant> right = constantElements(inst.getOperand(1));
    if (left == null || right == null || left.size() != right.size()) return null;
    List<Constant> result = new ArrayList<>();
    for (int lane = 0; lane < left.size(); lane++) {
      Long lhs = integerValue(left.get(lane));
      Long rhs = integerValue(right.get(lane));
      if (lhs == null || rhs == null) return null;
      Boolean folded = evalICmp(inst.getPredicate(), lhs, rhs);
      if (folded == null) return null;
      result.add(Constant.boolConst(folded));
    }
    return packConstant(inst.getType(), result);
  }

  private static Value foldFloatCompare(Instruction inst) {
    List<Constant> left = constantElements(inst.getOperand(0));
    List<Constant> right = constantElements(inst.getOperand(1));
    if (left == null || right == null || left.size() != right.size()) return null;
    List<Constant> result = new ArrayList<>();
    for (int lane = 0; lane < left.size(); lane++) {
      java.lang.Float lhs = floatingValue(left.get(lane));
      java.lang.Float rhs = floatingValue(right.get(lane));
      if (lhs == null || rhs == null) return null;
      Boolean folded = evalFCmp(inst.getPredicate(), lhs, rhs);
      if (folded == null) return null;
      result.add(Constant.boolConst(folded));
    }
    return packConstant(inst.getType(), result);
  }

  private static Value foldConversion(Instruction inst) {
    List<Constant> operands = constantElements(inst.getOperand(0));
    if (operands == null) return null;
    Type source = scalarType(inst.getOperand(0).getType());
    Type destination = scalarType(inst.getType());
    List<Constant> result = new ArrayList<>();
    for (Constant operand : operands) {
      Constant converted = convertConstant(inst.getOpcode(), operand, source, destination);
      if (converted == null) return null;
      result.add(converted);
    }
    return packConstant(inst.getType(), result);
  }

  private static Value foldBuildVector(Instruction inst) {
    List<Constant> elements = new ArrayList<>();
    for (int index = 0; index < inst.getNumOperands(); index++) {
      if (!(inst.getOperand(index) instanceof Constant constant)
          || constant.getType().isVector()) return null;
      elements.add(constant);
    }
    return Constant.vector(inst.getType(), elements);
  }

  private static Value foldSplat(Instruction inst) {
    if (!(inst.getOperand(0) instanceof Constant constant)
        || constant.getType().isVector()) return null;
    return Constant.vector(
        inst.getType(), Collections.nCopies(inst.getType().getLaneCount(), constant));
  }

  private static Value foldExtractElement(Instruction inst) {
    List<Constant> vector = constantElements(inst.getOperand(0));
    if (vector == null || !(inst.getOperand(1) instanceof Constant indexConstant)) return null;
    Long index = integerValue(indexConstant);
    return index == null || index < 0 || index >= vector.size()
        ? null : vector.get(index.intValue());
  }

  private static Value foldInsertElement(Instruction inst) {
    List<Constant> vector = constantElements(inst.getOperand(0));
    if (vector == null
        || !(inst.getOperand(1) instanceof Constant element)
        || !(inst.getOperand(2) instanceof Constant indexConstant)) return null;
    Long index = integerValue(indexConstant);
    if (index == null || index < 0 || index >= vector.size()) return null;
    List<Constant> result = new ArrayList<>(vector);
    result.set(index.intValue(), element);
    return Constant.vector(inst.getType(), result);
  }

  private static Value foldShuffleVector(Instruction inst) {
    List<Constant> left = constantElements(inst.getOperand(0));
    List<Constant> right = constantElements(inst.getOperand(1));
    if (left == null || right == null || !(inst.getOperand(2) instanceof Constant.Vector mask))
      return null;
    List<Constant> choices = new ArrayList<>(left);
    choices.addAll(right);
    List<Constant> result = new ArrayList<>();
    for (Constant maskElement : mask.elements) {
      Long index = integerValue(maskElement);
      if (index == null || index < 0 || index >= choices.size()) return null;
      result.add(choices.get(index.intValue()));
    }
    return Constant.vector(inst.getType(), result);
  }

  private static Constant packConstant(Type type, List<Constant> elements) {
    if (type.isVector()) return Constant.vector(type, elements);
    return elements.size() == 1 && elements.getFirst().getType().equals(type)
        ? elements.getFirst() : null;
  }

  private static List<Constant> constantElements(Value value) {
    if (!(value instanceof Constant constant)) return null;
    Type type = constant.getType();
    if (!type.isVector()) return List.of(constant);
    if (constant instanceof Constant.Vector vector) return vector.elements;
    if (constant instanceof Constant.Zero) {
      return Collections.nCopies(type.getLaneCount(), scalarZero(type.getElementType()));
    }
    return null;
  }

  private static Type scalarType(Type type) {
    return type.isVector() ? type.getElementType() : type;
  }

  private static Constant integerConstant(Type type, long value) {
    if (type == Type.I1) return Constant.boolConst((value & 1) != 0);
    if (type == Type.I64) return Constant.int64Const(value);
    return Constant.intConst((int) value);
  }

  private static Constant scalarZero(Type type) {
    return type == Type.FLOAT ? Constant.floatConst(0.0f) : integerConstant(type, 0);
  }

  private static Long integerValue(Constant constant) {
    if (constant instanceof Constant.Int integer) {
      if (constant.getType() == Type.I1) return integer.value == 0 ? 0L : 1L;
      if (constant.getType() == Type.INT) return (long) (int) integer.value;
      return integer.value;
    }
    return constant instanceof Constant.Zero && constant.getType().isInteger() ? 0L : null;
  }

  private static java.lang.Float floatingValue(Constant constant) {
    if (constant instanceof Constant.Float floating) return floating.value;
    return constant instanceof Constant.Zero && constant.getType() == Type.FLOAT ? 0.0f : null;
  }

  private static Long evalInt(Opcode opcode, Type type, long lhs, long rhs) {
    return switch (opcode) {
      case ADD -> lhs + rhs;
      case SUB -> lhs - rhs;
      case MUL -> lhs * rhs;
      case SMULH -> ((long) (int) lhs * (long) (int) rhs) >> Integer.SIZE;
      case SDIV -> rhs == 0 ? null : lhs / rhs;
      case SREM -> rhs == 0 ? null : lhs % rhs;
      case SHL -> shift(type, lhs, rhs, true);
      case ASHR -> shift(type, lhs, rhs, false);
      case AND -> lhs & rhs;
      case XOR -> lhs ^ rhs;
      default -> null;
    };
  }

  private static Long shift(Type type, long value, long amount, boolean left) {
    int width = type == Type.I64 ? Long.SIZE : type == Type.INT ? Integer.SIZE : 1;
    if (amount < 0 || amount >= width) return null;
    if (type == Type.I64) return left ? value << amount : value >> amount;
    return left ? (long) ((int) value << amount) : (long) ((int) value >> amount);
  }

  private static Boolean evalICmp(String predicate, long lhs, long rhs) {
    return switch (predicate) {
      case "eq" -> lhs == rhs;
      case "ne" -> lhs != rhs;
      case "slt" -> lhs < rhs;
      case "sle" -> lhs <= rhs;
      case "sgt" -> lhs > rhs;
      case "sge" -> lhs >= rhs;
      default -> null;
    };
  }

  private static Boolean evalFCmp(String predicate, float lhs, float rhs) {
    boolean unordered = Float.isNaN(lhs) || Float.isNaN(rhs);
    return switch (predicate) {
      case "oeq" -> !unordered && lhs == rhs;
      case "one" -> !unordered && lhs != rhs;
      case "olt" -> !unordered && lhs < rhs;
      case "ole" -> !unordered && lhs <= rhs;
      case "ogt" -> !unordered && lhs > rhs;
      case "oge" -> !unordered && lhs >= rhs;
      case "ueq" -> unordered || lhs == rhs;
      case "une" -> unordered || lhs != rhs;
      case "ult" -> unordered || lhs < rhs;
      case "ule" -> unordered || lhs <= rhs;
      case "ugt" -> unordered || lhs > rhs;
      case "uge" -> unordered || lhs >= rhs;
      default -> null;
    };
  }

  private static Constant convertConstant(
      Opcode opcode, Constant constant, Type source, Type destination) {
    return switch (opcode) {
      case ZEXT -> {
        Long value = integerValue(constant);
        if (value == null) yield null;
        long extended = source == Type.INT && destination == Type.I64
            ? Integer.toUnsignedLong((int) (long) value) : value;
        yield integerConstant(destination, extended);
      }
      case SEXT -> {
        Long value = integerValue(constant);
        if (value == null) yield null;
        long extended = source == Type.I1 ? (value == 0 ? 0 : -1)
            : source == Type.INT && destination == Type.I64 ? (int) (long) value : value;
        yield integerConstant(destination, extended);
      }
      case SITOFP -> {
        Long value = integerValue(constant);
        if (value != null && source == Type.I1 && value != 0) value = -1L;
        yield value == null || destination != Type.FLOAT
            ? null : Constant.floatConst((float) (long) value);
      }
      case FPTOSI -> {
        java.lang.Float value = floatingValue(constant);
        if (value == null || !Float.isFinite(value)) yield null;
        double widened = value;
        if (destination == Type.I1 && (widened < -1.0 || widened >= 1.0)) yield null;
        if (destination == Type.INT
            && (widened < Integer.MIN_VALUE || widened >= 2147483648.0)) yield null;
        if (destination == Type.I64
            && (widened < Long.MIN_VALUE || widened >= 9223372036854775808.0)) yield null;
        yield integerConstant(destination, destination == Type.INT ? (int) widened : (long) widened);
      }
      default -> null;
    };
  }
}
