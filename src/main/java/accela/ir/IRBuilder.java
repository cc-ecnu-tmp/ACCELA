package accela.ir;

import accela.backend.machine.VCIXInfo;
import accela.backend.machine.RVVConfig;
import accela.ir.Instruction.Opcode;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Builder API for constructing IR instructions. Manages the current insertion
 * point and automatically handles use-def chain maintenance.
 *
 * <p>Usage:
 *
 *   IRBuilder b = new IRBuilder();
 *   b.setInsertPoint(entryBlock);
 *   Instruction add = b.createAdd(lhs, rhs);
 *   b.createStore(add, ptr);
 *
 * <p>Vector arithmetic applies scalar broadcasting, numeric promotion, and right-zero-padding at
 * construction time. Other instruction factories remain intentionally lightweight; the IR
 * verifier is the final authority for type and structural correctness.
 */
public class IRBuilder {
  private BasicBlock insertBB;
  private Instruction insertBefore;

  public IRBuilder() {}

  public IRBuilder(BasicBlock bb) {
    this.insertBB = bb;
  }

  public void setInsertPoint(BasicBlock bb) {
    this.insertBB = bb;
    this.insertBefore = null;
  }

  /** Inserts subsequent instructions immediately before {@code inst}. */
  public void setInsertPointBefore(Instruction inst) {
    this.insertBB = inst.getParent();
    this.insertBefore = inst;
  }

  public BasicBlock getInsertBlock() {
    return insertBB;
  }

  /** Returns whether the current insertion block already ends in a terminator. */
  public boolean isTerminated() {
    return insertBB != null && insertBB.isTerminated();
  }

  /** Inserts an instruction into the current block and returns it for immediate use. */
  private Instruction insert(Instruction inst) {
    if (insertBefore != null) {
      insertBB.insertInstructionBefore(insertBefore, inst);
    } else {
      insertBB.addInstruction(inst);
    }
    return inst;
  }

  public Instruction createAdd(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.ADD, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.ADD, Opcode.FADD, lhs, rhs);
  }

  public Instruction createSub(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.SUB, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.SUB, Opcode.FSUB, lhs, rhs);
  }

  public Instruction createMul(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.MUL, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.MUL, Opcode.FMUL, lhs, rhs);
  }

  /** Returns the high 32 bits of the signed i32-by-i32 product. */
  public Instruction createSMulH(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.SMULH, lhs.getType(), lhs, rhs));
    }
    PromotedOperands promoted = promoteVectorOperands(lhs, rhs, false);
    if (promoted.type().getElementType() != Type.INT) {
      throw new IllegalArgumentException("vector SMULH requires i32 elements");
    }
    return insert(new Instruction(Opcode.SMULH, promoted.type(), promoted.left(), promoted.right()));
  }

  public Instruction createSDiv(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.SDIV, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.SDIV, Opcode.FDIV, lhs, rhs);
  }

  public Instruction createSRem(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.SREM, lhs.getType(), lhs, rhs));
    }
    return createPromotedIntegerBinary(Opcode.SREM, lhs, rhs);
  }

  public Instruction createShl(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.SHL, lhs.getType(), lhs, rhs));
    }
    return createPromotedIntegerBinary(Opcode.SHL, lhs, rhs);
  }

  public Instruction createAShr(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.ASHR, lhs.getType(), lhs, rhs));
    }
    return createPromotedIntegerBinary(Opcode.ASHR, lhs, rhs);
  }

  public Instruction createFAdd(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.FADD, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.ADD, Opcode.FADD, lhs, rhs);
  }

  public Instruction createFSub(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.FSUB, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.SUB, Opcode.FSUB, lhs, rhs);
  }

  public Instruction createFMul(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.FMUL, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.MUL, Opcode.FMUL, lhs, rhs);
  }

  public Instruction createFDiv(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.FDIV, lhs.getType(), lhs, rhs));
    }
    return createPromotedArithmetic(Opcode.SDIV, Opcode.FDIV, lhs, rhs);
  }

  public Instruction createFNeg(Value operand) {
    return insert(new Instruction(Opcode.FNEG, operand.getType(), operand));
  }

  public Instruction createICmp(String pred, Value lhs, Value rhs) {
    if (hasVectorOperand(lhs, rhs)) {
      PromotedOperands promoted = promoteVectorOperands(lhs, rhs, true);
      boolean floating = promoted.type().getElementType() == Type.FLOAT;
      Instruction inst = new Instruction(
          floating ? Opcode.FCMP : Opcode.ICMP,
          comparisonResultType(promoted.type()),
          promoted.left(),
          promoted.right());
      inst.setPredicate(floating ? intPredicateToFloat(pred) : pred);
      return insert(inst);
    }
    Instruction inst = new Instruction(Opcode.ICMP, comparisonResultType(lhs.getType()), lhs, rhs);
    inst.setPredicate(pred);
    return insert(inst);
  }

  public Instruction createFCmp(String pred, Value lhs, Value rhs) {
    if (hasVectorOperand(lhs, rhs)) {
      PromotedOperands promoted = promoteVectorOperands(lhs, rhs, true);
      boolean floating = promoted.type().getElementType() == Type.FLOAT;
      Instruction inst = new Instruction(
          floating ? Opcode.FCMP : Opcode.ICMP,
          comparisonResultType(promoted.type()),
          promoted.left(),
          promoted.right());
      inst.setPredicate(floating ? pred : floatPredicateToInt(pred));
      return insert(inst);
    }
    Instruction inst = new Instruction(Opcode.FCMP, comparisonResultType(lhs.getType()), lhs, rhs);
    inst.setPredicate(pred);
    return insert(inst);
  }

  public Instruction createAlloca(Type allocType) {
    Instruction inst = new Instruction(Opcode.ALLOCA, Type.PTR);
    inst.setAllocatedType(allocType);
    return insert(inst);
  }

  /** Creates an alloca directly in the entry block, regardless of the current insertion point. */
  public Instruction createAllocaInEntry(Type allocType, BasicBlock entryBB) {
    Instruction inst = new Instruction(Opcode.ALLOCA, Type.PTR);
    inst.setAllocatedType(allocType);
    entryBB.addInstructionAfterAllocas(inst);
    return inst;
  }

  public Instruction createLoad(Type type, Value ptr) {
    return insert(new Instruction(Opcode.LOAD, type, ptr));
  }

  public Instruction createStore(Value val, Value ptr) {
    return insert(new Instruction(Opcode.STORE, Type.VOID, val, ptr));
  }

  /**
   * Create a GEP instruction.
   *
   * @param sourceElemType the type being indexed into
   * @param ptr            the base pointer
   * @param indices        the index values
   * @param inbounds       whether to use inbounds
   */
  public Instruction createGEP(Type sourceElemType, Value ptr, Value[] indices, boolean inbounds) {
    Value[] operands = new Value[1 + indices.length];
    operands[0] = ptr;
    System.arraycopy(indices, 0, operands, 1, indices.length);
    Instruction inst = new Instruction(Opcode.GEP, Type.PTR, operands);
    inst.setGepSourceType(sourceElemType);
    inst.setGepInbounds(inbounds);
    return insert(inst);
  }

  public Instruction createBr(BasicBlock target) {
    return insert(new Instruction(Opcode.BR, Type.VOID, target));
  }

  public Instruction createCondBr(Value cond, BasicBlock ifTrue, BasicBlock ifFalse) {
    return insert(new Instruction(Opcode.CONDBR, Type.VOID, cond, ifTrue, ifFalse));
  }

  public Instruction createRet(Value val) {
    return insert(new Instruction(Opcode.RET, Type.VOID, val));
  }

  public Instruction createRetVoid() {
    return insert(new Instruction(Opcode.RET, Type.VOID));
  }

  public Instruction createZExt(Value val, Type destType) {
    return insert(new Instruction(Opcode.ZEXT, destType, val));
  }

  public Instruction createSExt(Value val, Type destType) {
    return insert(new Instruction(Opcode.SEXT, destType, val));
  }

  public Instruction createSIToFP(Value val, Type destType) {
    return insert(new Instruction(Opcode.SITOFP, destType, val));
  }

  public Instruction createFPToSI(Value val, Type destType) {
    return insert(new Instruction(Opcode.FPTOSI, destType, val));
  }

  /** Builds a vector from exactly one scalar operand per lane. */
  public Instruction createBuildVector(Type vectorType, Value... elements) {
    if (!vectorType.isVector()) throw new IllegalArgumentException("expected vector type");
    return insert(new Instruction(Opcode.BUILD_VECTOR, vectorType, elements));
  }

  /** Broadcasts one scalar value to all lanes of {@code vectorType}. */
  public Instruction createSplat(Type vectorType, Value scalar) {
    if (!vectorType.isVector()) throw new IllegalArgumentException("expected vector type");
    return insert(new Instruction(Opcode.SPLAT, vectorType, scalar));
  }

  /** Extracts one lane; the index is an integer scalar. */
  public Instruction createExtractElement(Value vector, Value index) {
    if (!vector.getType().isVector()) throw new IllegalArgumentException("expected vector value");
    return insert(new Instruction(Opcode.EXTRACT_ELEMENT, vector.getType().getElementType(), vector, index));
  }

  /** Returns a vector with one lane replaced. */
  public Instruction createInsertElement(Value vector, Value element, Value index) {
    if (!vector.getType().isVector()) throw new IllegalArgumentException("expected vector value");
    return insert(new Instruction(Opcode.INSERT_ELEMENT, vector.getType(), vector, element, index));
  }

  /** Selects lanes from two equal input vectors according to an i32 vector mask. */
  public Instruction createShuffleVector(Value lhs, Value rhs, Constant.Vector mask) {
    if (!lhs.getType().isVector()) throw new IllegalArgumentException("expected vector value");
    Type resultType = Type.vector(lhs.getType().getElementType(), mask.getType().getLaneCount());
    return insert(new Instruction(Opcode.SHUFFLE_VECTOR, resultType, lhs, rhs, mask));
  }

  public Instruction createXor(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.XOR, lhs.getType(), lhs, rhs));
    }
    return createPromotedIntegerBinary(Opcode.XOR, lhs, rhs);
  }

  /** Creates an explicit SiFive VCIX intrinsic; no source-language syntax is implied. */
  public Instruction createVCIX(Type resultType, VCIXInfo info, Value... operands) {
    return createVCIX(resultType, info, null, operands);
  }

  /** Creates VCIX with an explicit configuration, required by forms without vector values. */
  public Instruction createVCIX(
      Type resultType, VCIXInfo info, RVVConfig config, Value... operands) {
    if (info == null) throw new IllegalArgumentException("VCIX encoding info is required");
    if (resultType == null) resultType = Type.VOID;
    if (info.writesVectorDestination() != resultType.isVector()) {
      throw new IllegalArgumentException(
          "VCIX destination flag and intrinsic result type do not agree");
    }
    Instruction instruction = new Instruction(Opcode.VCIX, resultType, operands);
    instruction.setVCIXInfo(info);
    instruction.setVCIXConfig(config);
    return insert(instruction);
  }

  public Instruction createAnd(Value lhs, Value rhs) {
    if (!hasVectorOperand(lhs, rhs)) {
      return insert(new Instruction(Opcode.AND, lhs.getType(), lhs, rhs));
    }
    return createPromotedIntegerBinary(Opcode.AND, lhs, rhs);
  }

  /** Clones a side-effect-free binary expression while preserving its result type. */
  public Instruction createBinary(Opcode opcode, Value lhs, Value rhs) {
    return switch (opcode) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR,
          FADD, FSUB, FMUL, FDIV ->
          insert(new Instruction(opcode, lhs.getType(), lhs, rhs));
      default -> throw new IllegalArgumentException("not a binary expression: " + opcode);
    };
  }

  /**
   * Create a function call.
   *
   * @param callee   the called function
   * @param retType  return type
   * @param args     argument values
   */
  public Instruction createCall(Function callee, Type retType, Value... args) {
    Instruction inst = new Instruction(Opcode.CALL, retType, args);
    inst.setCallee(callee);
    return insert(inst);
  }

  public Instruction createPhi(Type type) {
    return insert(new Instruction(Opcode.PHI, type));
  }

  /** Selects one of two equal-typed scalar values from an i1 condition. */
  public Instruction createSelect(Value condition, Value ifTrue, Value ifFalse) {
    if (!ifTrue.getType().equals(ifFalse.getType())) {
      throw new IllegalArgumentException("select values must have the same type");
    }
    return insert(new Instruction(Opcode.SELECT, ifTrue.getType(), condition, ifTrue, ifFalse));
  }

  /**
   * Applies APL-style scalar extension and numeric promotion for a vector arithmetic operation.
   * Scalars are repeated to every lane; shorter vectors are padded with zeros on the right.
   */
  private Instruction createPromotedArithmetic(
      Opcode integerOpcode, Opcode floatOpcode, Value lhs, Value rhs) {
    PromotedOperands promoted = promoteVectorOperands(lhs, rhs, true);
    Opcode opcode = promoted.type().getElementType() == Type.FLOAT
        ? floatOpcode : integerOpcode;
    return insert(new Instruction(opcode, promoted.type(), promoted.left(), promoted.right()));
  }

  private Instruction createPromotedIntegerBinary(Opcode opcode, Value lhs, Value rhs) {
    PromotedOperands promoted = promoteVectorOperands(lhs, rhs, false);
    return insert(new Instruction(opcode, promoted.type(), promoted.left(), promoted.right()));
  }

  private PromotedOperands promoteVectorOperands(Value originalLeft, Value originalRight,
      boolean allowFloat) {
    Value left = canonicalizeIntegralFloatConstant(originalLeft);
    Value right = canonicalizeIntegralFloatConstant(originalRight);
    Type elementType = commonElementType(left.getType(), right.getType());
    if (!allowFloat && elementType == Type.FLOAT) {
      throw new IllegalArgumentException("floating-point operand is invalid for integer vector op");
    }
    int lanes = Math.max(laneCount(left.getType()), laneCount(right.getType()));
    Type resultType = Type.vector(elementType, lanes);
    Value promotedLeft = normalizeVectorOperand(left, elementType, lanes);
    Value promotedRight = normalizeVectorOperand(right, elementType, lanes);
    return new PromotedOperands(promotedLeft, promotedRight, resultType);
  }

  private Value normalizeVectorOperand(Value value, Type elementType, int lanes) {
    Value converted = convertElementType(value, elementType);
    if (!converted.getType().isVector()) return broadcastScalar(converted, elementType, lanes);
    return padVector(converted, lanes);
  }

  private Value broadcastScalar(Value scalar, Type elementType, int lanes) {
    Type vectorType = Type.vector(elementType, lanes);
    if (scalar instanceof Constant constant) {
      return Constant.vector(vectorType, Collections.nCopies(lanes, constant));
    }
    return createSplat(vectorType, scalar);
  }

  private Value padVector(Value vector, int lanes) {
    int sourceLanes = vector.getType().getLaneCount();
    if (sourceLanes == lanes) return vector;
    Type resultType = Type.vector(vector.getType().getElementType(), lanes);
    if (vector instanceof Constant.Vector constant) {
      List<Constant> elements = new ArrayList<>(constant.elements);
      Constant zero = scalarZero(vector.getType().getElementType());
      while (elements.size() < lanes) elements.add(zero);
      return Constant.vector(resultType, elements);
    }
    if (vector instanceof Constant.Zero) return Constant.zero(resultType);

    List<Constant> indices = new ArrayList<>();
    for (int lane = 0; lane < lanes; lane++) {
      // The second input is all zero, so reusing its first lane pads an arbitrary tail length.
      indices.add(Constant.intConst(lane < sourceLanes ? lane : sourceLanes));
    }
    Constant.Vector mask = Constant.vector(Type.vector(Type.INT, lanes), indices);
    return createShuffleVector(vector, Constant.zero(vector.getType()), mask);
  }

  private Value convertElementType(Value value, Type destinationElement) {
    Type sourceElement = elementType(value.getType());
    if (sourceElement == destinationElement) return value;
    if (value instanceof Constant.Vector vector) {
      List<Constant> converted = vector.elements.stream()
          .map(element -> convertScalarConstant(element, destinationElement))
          .toList();
      return Constant.vector(Type.vector(destinationElement, vector.getType().getLaneCount()), converted);
    }
    if (value instanceof Constant.Zero) {
      Type destination = value.getType().isVector()
          ? Type.vector(destinationElement, value.getType().getLaneCount()) : destinationElement;
      return Constant.zero(destination);
    }
    if (value instanceof Constant constant) {
      return convertScalarConstant(constant, destinationElement);
    }

    Type destination = value.getType().isVector()
        ? Type.vector(destinationElement, value.getType().getLaneCount()) : destinationElement;
    if (sourceElement.isInteger() && destinationElement == Type.FLOAT) {
      return createSIToFP(value, destination);
    }
    if (sourceElement == Type.I1 && destinationElement.isInteger()) {
      return createZExt(value, destination);
    }
    if (sourceElement == Type.INT && destinationElement == Type.I64) {
      return createSExt(value, destination);
    }
    throw new IllegalArgumentException(
        "unsupported vector element conversion from " + sourceElement + " to " + destinationElement);
  }

  private static Constant convertScalarConstant(Constant constant, Type destination) {
    if (constant.getType() == destination) return constant;
    if (destination == Type.FLOAT) {
      if (constant instanceof Constant.Int integer) return Constant.floatConst((float) integer.value);
      if (constant instanceof Constant.Zero) return Constant.floatConst(0.0f);
    }
    if (destination == Type.I64) {
      if (constant instanceof Constant.Int integer) return Constant.int64Const(integer.value);
      if (constant instanceof Constant.Float floating) {
        java.lang.Integer exact = Constant.exactI32(floating.value);
        if (exact != null) return Constant.int64Const(exact);
      }
      if (constant instanceof Constant.Zero) return Constant.int64Const(0);
    }
    if (destination == Type.INT) {
      if (constant instanceof Constant.Int integer) return Constant.intConst(integer.value);
      if (constant instanceof Constant.Float floating) {
        java.lang.Integer exact = Constant.exactI32(floating.value);
        if (exact != null) return Constant.intConst(exact);
      }
      if (constant instanceof Constant.Zero) return Constant.intConst(0);
    }
    if (destination == Type.I1) {
      if (constant instanceof Constant.Int integer && (integer.value == 0 || integer.value == 1)) {
        return Constant.boolConst(integer.value != 0);
      }
      if (constant instanceof Constant.Zero) return Constant.boolConst(false);
    }
    throw new IllegalArgumentException(
        "constant cannot be converted from " + constant.getType() + " to " + destination);
  }

  /** Treat exactly integral float constants as integers only in a vector-operation context. */
  private static Value canonicalizeIntegralFloatConstant(Value value) {
    if (value instanceof Constant.Float floating) {
      java.lang.Integer exact = Constant.exactI32(floating.value);
      return exact == null ? value : Constant.intConst(exact);
    }
    if (value instanceof Constant.Zero zero && zero.getType() == Type.FLOAT) {
      return Constant.intConst(0);
    }
    if (value instanceof Constant.Zero zero
        && zero.getType().isVector()
        && zero.getType().getElementType() == Type.FLOAT) {
      return Constant.zero(Type.vector(Type.INT, zero.getType().getLaneCount()));
    }
    if (!(value instanceof Constant.Vector vector)
        || vector.getType().getElementType() != Type.FLOAT) return value;

    List<Constant> integers = new ArrayList<>();
    for (Constant element : vector.elements) {
      if (element instanceof Constant.Zero) {
        integers.add(Constant.intConst(0));
        continue;
      }
      if (!(element instanceof Constant.Float floating)) return value;
      java.lang.Integer exact = Constant.exactI32(floating.value);
      if (exact == null) return value;
      integers.add(Constant.intConst(exact));
    }
    return Constant.vector(Type.vector(Type.INT, integers.size()), integers);
  }

  private static Type commonElementType(Type left, Type right) {
    Type leftElement = elementType(left);
    Type rightElement = elementType(right);
    if ((!leftElement.isInteger() && leftElement != Type.FLOAT)
        || (!rightElement.isInteger() && rightElement != Type.FLOAT)) {
      throw new IllegalArgumentException("element-wise operations require numeric operands");
    }
    if (leftElement == Type.FLOAT || rightElement == Type.FLOAT) return Type.FLOAT;
    if (leftElement == Type.I64 || rightElement == Type.I64) return Type.I64;
    if (leftElement == Type.INT || rightElement == Type.INT) return Type.INT;
    return Type.I1;
  }

  private static Type elementType(Type type) {
    return type.isVector() ? type.getElementType() : type;
  }

  private static int laneCount(Type type) {
    return type.isVector() ? type.getLaneCount() : 1;
  }

  private static boolean hasVectorOperand(Value lhs, Value rhs) {
    return lhs.getType().isVector() || rhs.getType().isVector();
  }

  private static Constant scalarZero(Type type) {
    if (type == Type.FLOAT) return Constant.floatConst(0.0f);
    if (type == Type.I64) return Constant.int64Const(0);
    if (type == Type.I1) return Constant.boolConst(false);
    return Constant.intConst(0);
  }

  private static String intPredicateToFloat(String predicate) {
    return switch (predicate) {
      case "eq" -> "oeq";
      case "ne" -> "une";
      case "slt" -> "olt";
      case "sle" -> "ole";
      case "sgt" -> "ogt";
      case "sge" -> "oge";
      default -> throw new IllegalArgumentException("unsupported integer predicate: " + predicate);
    };
  }

  private static String floatPredicateToInt(String predicate) {
    return switch (predicate) {
      case "oeq", "ueq" -> "eq";
      case "one", "une" -> "ne";
      case "olt", "ult" -> "slt";
      case "ole", "ule" -> "sle";
      case "ogt", "ugt" -> "sgt";
      case "oge", "uge" -> "sge";
      default -> throw new IllegalArgumentException("unsupported float predicate: " + predicate);
    };
  }

  private record PromotedOperands(Value left, Value right, Type type) {}

  private static Type comparisonResultType(Type operandType) {
    return operandType.isVector() ? Type.vector(Type.I1, operandType.getLaneCount()) : Type.I1;
  }

}
