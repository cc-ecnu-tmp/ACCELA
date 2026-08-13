package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.FunctionAnalysis;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.scev.SCEV;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;

/**
 * Computes canonical symbolic scalar expressions, affine loop recurrences, and simple exact exit
 * counts.
 */
public final class ScalarEvolutionAnalysis
    implements FunctionAnalysis<ScalarEvolutionAnalysis.Result> {
  public static final class Result {
    private static final BigInteger ZERO = BigInteger.ZERO;
    private static final BigInteger ONE = BigInteger.ONE;
    private static final BigInteger NEGATIVE_ONE = BigInteger.ONE.negate();

    private final Function function;
    private final LoopAnalysis.Result loops;
    private final DominatorTreeAnalysis.Result dominators;
    private final Map<Value, SCEV> values = new IdentityHashMap<>();
    private final Set<Value> beingAnalyzed =
        java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    private final Set<LoopAnalysis.Loop> computingExitCounts =
        java.util.Collections.newSetFromMap(new IdentityHashMap<>());
    private final Map<LoopAnalysis.Loop, Optional<BigInteger>> exitCountCache =
        new IdentityHashMap<>();
    private final Map<SCEV, SCEV> uniqueExpressions = new HashMap<>();
    private final Map<SCEV, Integer> expressionIds = new IdentityHashMap<>();
    private int nextExpressionId;

    private Result(
        Function function,
        LoopAnalysis.Result loops,
        DominatorTreeAnalysis.Result dominators) {
      this.function = function;
      this.loops = loops;
      this.dominators = dominators;
    }

    public Function getFunction() {
      return function;
    }

    public LoopAnalysis.Result getLoopInfo() {
      return loops;
    }

    /** Returns a canonical symbolic expression for one SSA value. */
    public SCEV getSCEV(Value value) {
      SCEV cached = values.get(value);
      if (cached != null) return cached;
      if (!isSCEVable(value.getType()) || !beingAnalyzed.add(value)) {
        return getUnknown(value);
      }
      SCEV expression;
      try {
        expression = createSCEV(value);
      } finally {
        beingAnalyzed.remove(value);
      }
      values.put(value, expression);
      return expression;
    }

    public SCEV.Constant getConstant(Type type, long value) {
      return getConstant(type, BigInteger.valueOf(value));
    }

    public SCEV.Constant getConstant(Type type, BigInteger value) {
      return intern(new SCEV.Constant(type, normalizeSigned(value, type)));
    }

    public SCEV getAddExpr(SCEV... operands) {
      return getAddExpr(List.of(operands));
    }

    public SCEV getMulExpr(SCEV... operands) {
      return getMulExpr(List.of(operands));
    }

    public SCEV getNegative(SCEV operand) {
      return getMulExpr(getConstant(operand.getType(), NEGATIVE_ONE), operand);
    }

    /**
     * Returns the additive delta when {@code backedgeValue} is syntactically {@code phi + delta}.
     *
     * <p>This is the same scalar-evolution decomposition used to recognize ordinary affine
     * add-recurrences. The returned delta is not required to be loop invariant, allowing clients
     * to ask the shared SCEV implementation whether it is an iteration polynomial. Multiplicative
     * or subtractive occurrences of {@code phi}, multiple occurrences, and cyclic expression
     * graphs are rejected.
     */
    public Optional<SCEV> getAdditiveRecurrenceDelta(
        Instruction phi, Value backedgeValue, LoopAnalysis.Loop loop) {
      if (phi == null || backedgeValue == null || loop == null) {
        throw new NullPointerException();
      }
      if (phi.getOpcode() != Instruction.Opcode.PHI
          || phi.getParent() != loop.header()
          || phi.getType() != backedgeValue.getType()) {
        return Optional.empty();
      }
      return Optional.ofNullable(
          extractAdditiveDelta(backedgeValue, phi, loop, new LinkedHashSet<>()));
    }

    /**
     * Projects an i32 SCEV onto a degree-at-most-two polynomial in {@code loop}'s zero-based
     * iteration number.
     *
     * <p>The operation is exact in the i32 residue ring: addition uses coefficient-wise addition
     * and multiplication uses convolution. Expressions with a non-invariant coefficient, an
     * unsupported operation, or a nonzero term above degree two are rejected rather than
     * truncated.
     */
    public Optional<SCEV.IterationPolynomial> getIntegerIterationPolynomial(
        SCEV expression, LoopAnalysis.Loop loop) {
      Objects.requireNonNull(expression, "expression");
      Objects.requireNonNull(loop, "loop");
      if (expression.getType() != Type.INT) return Optional.empty();
      List<SCEV> coefficients = projectIntegerPolynomial(expression, loop);
      if (coefficients == null) return Optional.empty();
      return Optional.of(intern(new SCEV.IterationPolynomial(Type.INT, coefficients, loop)));
    }

    /** Whether the expression remains fixed during one execution of {@code loop}. */
    public boolean isLoopInvariant(SCEV expression, LoopAnalysis.Loop loop) {
      if (expression instanceof SCEV.Constant) return true;
      if (expression instanceof SCEV.Unknown unknown) {
        return isValueLoopInvariant(unknown.value(), loop);
      }
      if (expression instanceof SCEV.Add add) {
        return add.operands().stream().allMatch(operand -> isLoopInvariant(operand, loop));
      }
      if (expression instanceof SCEV.Multiply multiply) {
        return multiply.operands().stream().allMatch(operand -> isLoopInvariant(operand, loop));
      }
      if (expression instanceof SCEV.SignedDivide divide) {
        return isLoopInvariant(divide.dividend(), loop)
            && isLoopInvariant(divide.divisor(), loop);
      }
      if (expression instanceof SCEV.ZeroExtend extend) {
        return isLoopInvariant(extend.operand(), loop);
      }
      if (expression instanceof SCEV.SignExtend extend) {
        return isLoopInvariant(extend.operand(), loop);
      }
      if (expression instanceof SCEV.PointerAdd pointerAdd) {
        return isLoopInvariant(pointerAdd.base(), loop)
            && isLoopInvariant(pointerAdd.byteOffset(), loop);
      }
      if (expression instanceof SCEV.IterationPolynomial polynomial) {
        return polynomial.loop() != loop
            && !loop.blocks().containsAll(polynomial.loop().blocks());
      }
      SCEV.AddRec recurrence = (SCEV.AddRec) expression;
      return recurrence.loop() != loop
          && !loop.blocks().containsAll(recurrence.loop().blocks());
    }

    /** Evaluates an affine recurrence at a symbolic iteration number. */
    public SCEV evaluateAtIteration(SCEV.AddRec recurrence, SCEV iteration) {
      SCEV scaledStep = getMulExpr(recurrence.step(), iteration);
      return recurrence.getType().isPointer()
          ? getPointerAdd(recurrence.start(), scaledStep)
          : getAddExpr(recurrence.start(), scaledStep);
    }

    /**
     * Returns the exact number of back-edge traversals for a canonical constant-bound loop.
     * Empty means that the exit is unsupported or cannot be proven finite without wraparound.
     */
    public Optional<BigInteger> getConstantBackedgeTakenCount(LoopAnalysis.Loop loop) {
      Optional<BigInteger> cached = exitCountCache.get(loop);
      if (cached != null) return cached;
      if (!computingExitCounts.add(loop)) return Optional.empty();
      try {
        Optional<BigInteger> result = computeConstantBackedgeTakenCount(loop);
        exitCountCache.put(loop, result);
        return result;
      } finally {
        computingExitCounts.remove(loop);
      }
    }

    private Optional<BigInteger> computeConstantBackedgeTakenCount(LoopAnalysis.Loop loop) {
      Set<BasicBlock> exitingBlocks = getExitingBlocks(loop);
      if (exitingBlocks.size() != 1) return Optional.empty();
      BasicBlock exiting = exitingBlocks.iterator().next();
      for (BasicBlock latch : loop.latches()) {
        if (!dominators.dominates(exiting, latch)) return Optional.empty();
      }
      Instruction branch = exiting.getTerminator();
      if (branch == null
          || branch.getOpcode() != Instruction.Opcode.CONDBR
          || !(branch.getOperand(0) instanceof Instruction compare)
          || compare.getOpcode() != Instruction.Opcode.ICMP) {
        return Optional.empty();
      }

      boolean trueInside = loop.contains((BasicBlock) branch.getOperand(1));
      boolean falseInside = loop.contains((BasicBlock) branch.getOperand(2));
      if (trueInside == falseInside) return Optional.empty();

      String predicate = compare.getPredicate();
      if (!trueInside) predicate = invertPredicate(predicate);
      if (predicate == null) return Optional.empty();

      SCEV left = getSCEV(compare.getOperand(0));
      SCEV right = getSCEV(compare.getOperand(1));
      SCEV.AddRec recurrence;
      SCEV bound;
      if (left instanceof SCEV.AddRec addRec && addRec.loop() == loop) {
        recurrence = addRec;
        bound = right;
      } else if (right instanceof SCEV.AddRec addRec && addRec.loop() == loop) {
        recurrence = addRec;
        bound = left;
        predicate = swapPredicate(predicate);
      } else {
        return Optional.empty();
      }
      if (predicate == null
          || !(recurrence.start() instanceof SCEV.Constant start)
          || !(recurrence.step() instanceof SCEV.Constant step)
          || !(bound instanceof SCEV.Constant limit)) {
        return Optional.empty();
      }
      return solveExitCount(start.value(), step.value(), limit.value(), predicate, recurrence.getType());
    }

    private SCEV createSCEV(Value value) {
      if (value instanceof Constant.Int integer) {
        return getConstant(value.getType(), BigInteger.valueOf(integer.value));
      }
      if (!(value instanceof Instruction instruction)) return getUnknown(value);
      return switch (instruction.getOpcode()) {
        case ADD -> getAddExpr(getSCEV(instruction.getOperand(0)), getSCEV(instruction.getOperand(1)));
        case SUB -> getAddExpr(
            getSCEV(instruction.getOperand(0)), getNegative(getSCEV(instruction.getOperand(1))));
        case MUL -> getMulExpr(getSCEV(instruction.getOperand(0)), getSCEV(instruction.getOperand(1)));
        case SDIV -> getSignedDivide(
            getSCEV(instruction.getOperand(0)), getSCEV(instruction.getOperand(1)));
        case ZEXT -> getZeroExtend(getSCEV(instruction.getOperand(0)), instruction.getType());
        case SEXT -> getSignExtend(getSCEV(instruction.getOperand(0)), instruction.getType());
        case GEP -> getGEP(instruction);
        case PHI -> createAddRecFromPhi(instruction);
        default -> getUnknown(value);
      };
    }

    private SCEV createAddRecFromPhi(Instruction phi) {
      LoopAnalysis.Loop loop = loops.getLoopFor(phi.getParent());
      if (loop == null || loop.header() != phi.getParent() || phi.getNumOperands() % 2 != 0) {
        return getUnknown(phi);
      }
      Value startValue = null;
      List<Value> backedgeValues = new ArrayList<>();
      for (int index = 0; index < phi.getNumOperands(); index += 2) {
        if (!(phi.getOperand(index + 1) instanceof BasicBlock predecessor)) {
          return getUnknown(phi);
        }
        if (loop.contains(predecessor)) {
          backedgeValues.add(phi.getOperand(index));
        } else if (startValue == null) {
          startValue = phi.getOperand(index);
        } else {
          return getUnknown(phi);
        }
      }
      if (startValue == null || backedgeValues.isEmpty()) return getUnknown(phi);

      SCEV commonStep = null;
      for (Value backedgeValue : backedgeValues) {
        SCEV step = getAdditiveRecurrenceDelta(phi, backedgeValue, loop).orElse(null);
        if (step == null || !isLoopInvariant(step, loop)) return getUnknown(phi);
        if (commonStep != null && !commonStep.equals(step)) return getUnknown(phi);
        commonStep = step;
      }
      SCEV start = getSCEV(startValue);
      if (commonStep == null || !isLoopInvariant(start, loop)) return getUnknown(phi);
      return getAddRec(phi.getType(), start, commonStep, loop);
    }

    private SCEV extractAdditiveDelta(
        Value value,
        Instruction phi,
        LoopAnalysis.Loop loop,
        Set<Value> visited) {
      if (value == phi) return getConstant(phi.getType(), ZERO);
      if (!visited.add(value) || !(value instanceof Instruction instruction)) return null;
      if (instruction.getOpcode() == Instruction.Opcode.ADD) {
        boolean leftDepends = dependsOnPhi(instruction.getOperand(0), phi, new LinkedHashSet<>());
        boolean rightDepends = dependsOnPhi(instruction.getOperand(1), phi, new LinkedHashSet<>());
        if (leftDepends == rightDepends) return null;
        if (leftDepends) {
          SCEV left = extractAdditiveDelta(
              instruction.getOperand(0), phi, loop, new LinkedHashSet<>(visited));
          if (left == null) return null;
          SCEV other = getSCEV(instruction.getOperand(1));
          return getAddExpr(left, other);
        }
        SCEV right = extractAdditiveDelta(
            instruction.getOperand(1), phi, loop, new LinkedHashSet<>(visited));
        if (right == null) return null;
        return getAddExpr(right, getSCEV(instruction.getOperand(0)));
      } else if (instruction.getOpcode() == Instruction.Opcode.SUB) {
        if (!dependsOnPhi(instruction.getOperand(0), phi, new LinkedHashSet<>())
            || dependsOnPhi(instruction.getOperand(1), phi, new LinkedHashSet<>())) return null;
        SCEV left = extractAdditiveDelta(
            instruction.getOperand(0), phi, loop, new LinkedHashSet<>(visited));
        if (left == null) return null;
        return getAddExpr(left, getNegative(getSCEV(instruction.getOperand(1))));
      }
      return null;
    }

    private static boolean dependsOnPhi(
        Value value, Instruction phi, Set<Value> visited) {
      if (value == phi) return true;
      if (!(value instanceof Instruction instruction) || !visited.add(value)) return false;
      if (instruction.getOpcode() == Instruction.Opcode.PHI) return false;
      for (int index = 0; index < instruction.getNumOperands(); index++) {
        if (dependsOnPhi(instruction.getOperand(index), phi, visited)) return true;
      }
      return false;
    }

    private List<SCEV> projectIntegerPolynomial(
        SCEV expression, LoopAnalysis.Loop loop) {
      if (expression.getType() != Type.INT) return null;
      if (expression instanceof SCEV.AddRec recurrence && recurrence.loop() == loop) {
        if (!isLoopInvariant(recurrence.start(), loop)
            || !isLoopInvariant(recurrence.step(), loop)) return null;
        return trimPolynomial(List.of(recurrence.start(), recurrence.step()));
      }
      if (expression instanceof SCEV.IterationPolynomial polynomial) {
        if (polynomial.loop() != loop
            || polynomial.coefficients().stream()
                .anyMatch(coefficient -> !isLoopInvariant(coefficient, loop))) return null;
        return polynomial.coefficients();
      }
      if (isLoopInvariant(expression, loop)) return List.of(expression);
      if (expression instanceof SCEV.Add add) {
        List<SCEV> result = List.of(getConstant(Type.INT, ZERO));
        for (SCEV operand : add.operands()) {
          List<SCEV> projected = projectIntegerPolynomial(operand, loop);
          if (projected == null) return null;
          result = addPolynomials(result, projected);
        }
        return trimPolynomial(result);
      }
      if (expression instanceof SCEV.Multiply multiply) {
        List<SCEV> result = List.of(getConstant(Type.INT, ONE));
        for (SCEV operand : multiply.operands()) {
          List<SCEV> projected = projectIntegerPolynomial(operand, loop);
          if (projected == null) return null;
          result = multiplyPolynomials(result, projected);
          if (result == null) return null;
        }
        return trimPolynomial(result);
      }
      return null;
    }

    private List<SCEV> addPolynomials(List<SCEV> left, List<SCEV> right) {
      int size = Math.max(left.size(), right.size());
      List<SCEV> result = new ArrayList<>(size);
      for (int index = 0; index < size; index++) {
        SCEV leftCoefficient = index < left.size()
            ? left.get(index) : getConstant(Type.INT, ZERO);
        SCEV rightCoefficient = index < right.size()
            ? right.get(index) : getConstant(Type.INT, ZERO);
        result.add(getAddExpr(leftCoefficient, rightCoefficient));
      }
      return result;
    }

    private List<SCEV> multiplyPolynomials(List<SCEV> left, List<SCEV> right) {
      SCEV zero = getConstant(Type.INT, ZERO);
      List<SCEV> result = new ArrayList<>(List.of(zero, zero, zero));
      for (int leftIndex = 0; leftIndex < left.size(); leftIndex++) {
        for (int rightIndex = 0; rightIndex < right.size(); rightIndex++) {
          int degree = leftIndex + rightIndex;
          SCEV product = getMulExpr(left.get(leftIndex), right.get(rightIndex));
          if (degree > 2) {
            if (!isZero(product)) return null;
            continue;
          }
          result.set(degree, getAddExpr(result.get(degree), product));
        }
      }
      return result;
    }

    private List<SCEV> trimPolynomial(List<SCEV> coefficients) {
      int size = coefficients.size();
      while (size > 1 && isZero(coefficients.get(size - 1))) size--;
      return List.copyOf(coefficients.subList(0, size));
    }

    private SCEV getGEP(Instruction gep) {
      SCEV base = getSCEV(gep.getOperand(0));
      Type indexedType = gep.getGepSourceType();
      if (indexedType == null) return getUnknown(gep);
      List<SCEV> offsets = new ArrayList<>();
      for (int index = 1; index < gep.getNumOperands(); index++) {
        Type elementType;
        if (index == 1) {
          elementType = indexedType;
        } else if (indexedType != null && indexedType.isArray()) {
          indexedType = indexedType.innerType;
          elementType = indexedType;
        } else {
          return getUnknown(gep);
        }
        SCEV scaledIndex = getMulExpr(
            castIndexToPointerWidth(getSCEV(gep.getOperand(index))),
            getConstant(Type.I64, allocationSize(elementType)));
        offsets.add(scaledIndex);
      }
      SCEV offset = offsets.isEmpty() ? getConstant(Type.I64, ZERO) : getAddExpr(offsets);
      return getPointerAdd(base, offset);
    }

    private SCEV castIndexToPointerWidth(SCEV index) {
      if (index.getType() == Type.I64) return index;
      if (index.getType() == Type.I1) return getZeroExtend(index, Type.I64);
      return getSignExtend(index, Type.I64);
    }

    private SCEV getPointerAdd(SCEV base, SCEV offset) {
      if (isZero(offset)) return base;
      if (base instanceof SCEV.PointerAdd pointerAdd) {
        return getPointerAdd(pointerAdd.base(), getAddExpr(pointerAdd.byteOffset(), offset));
      }
      if (offset instanceof SCEV.AddRec recurrence && isLoopInvariant(base, recurrence.loop())) {
        SCEV start = getPointerAdd(base, recurrence.start());
        return getAddRec(Type.PTR, start, recurrence.step(), recurrence.loop());
      }
      if (base instanceof SCEV.AddRec recurrence
          && recurrence.getType().isPointer()
          && isLoopInvariant(offset, recurrence.loop())) {
        SCEV start = getPointerAdd(recurrence.start(), offset);
        return getAddRec(Type.PTR, start, recurrence.step(), recurrence.loop());
      }
      return intern(new SCEV.PointerAdd(base, offset));
    }

    private SCEV getAddExpr(List<SCEV> input) {
      if (input.isEmpty()) throw new IllegalArgumentException("empty SCEV add");
      Type type = input.get(0).getType();
      List<SCEV> operands = new ArrayList<>();
      BigInteger constant = ZERO;
      for (SCEV rawOperand : input) {
        SCEV operand = intern(rawOperand);
        if (operand.getType() != type) {
          throw new IllegalArgumentException("SCEV add type mismatch");
        }
        if (operand instanceof SCEV.Add add && add.getType() == type) {
          operands.addAll(add.operands());
        } else {
          operands.add(operand);
        }
      }
      List<SCEV> nonConstants = new ArrayList<>();
      for (SCEV rawOperand : operands) {
        SCEV operand = intern(rawOperand);
        if (operand instanceof SCEV.Constant value) constant = constant.add(value.value());
        else nonConstants.add(operand);
      }
      constant = normalizeSigned(constant, type);
      if (!constant.equals(ZERO)) nonConstants.add(getConstant(type, constant));
      if (nonConstants.isEmpty()) return getConstant(type, ZERO);
      if (nonConstants.size() == 1) return nonConstants.get(0);

      SCEV foldedRecurrence = tryFoldAddRec(nonConstants, type);
      if (foldedRecurrence != null) return foldedRecurrence;
      nonConstants.sort(this::compareExpressions);
      return intern(new SCEV.Add(type, nonConstants));
    }

    private SCEV tryFoldAddRec(List<SCEV> operands, Type type) {
      LoopAnalysis.Loop recurrenceLoop = null;
      for (SCEV operand : operands) {
        if (operand instanceof SCEV.AddRec recurrence) {
          if (recurrenceLoop != null && recurrence.loop() != recurrenceLoop) return null;
          recurrenceLoop = recurrence.loop();
        }
      }
      if (recurrenceLoop == null) return null;
      List<SCEV> starts = new ArrayList<>();
      List<SCEV> steps = new ArrayList<>();
      for (SCEV operand : operands) {
        if (operand instanceof SCEV.AddRec recurrence) {
          starts.add(recurrence.start());
          steps.add(recurrence.step());
        } else if (isLoopInvariant(operand, recurrenceLoop)) {
          starts.add(operand);
        } else {
          return null;
        }
      }
      SCEV start = starts.size() == 1 ? starts.get(0) : getAddExpr(starts);
      SCEV step = steps.size() == 1 ? steps.get(0) : getAddExpr(steps);
      return getAddRec(type, start, step, recurrenceLoop);
    }

    private SCEV getMulExpr(List<SCEV> input) {
      if (input.isEmpty()) throw new IllegalArgumentException("empty SCEV multiply");
      Type type = input.get(0).getType();
      List<SCEV> operands = new ArrayList<>();
      BigInteger constant = ONE;
      for (SCEV rawOperand : input) {
        SCEV operand = intern(rawOperand);
        if (operand.getType() != type) {
          throw new IllegalArgumentException("SCEV multiply type mismatch");
        }
        if (operand instanceof SCEV.Multiply multiply && multiply.getType() == type) {
          operands.addAll(multiply.operands());
        } else {
          operands.add(operand);
        }
      }
      List<SCEV> nonConstants = new ArrayList<>();
      for (SCEV rawOperand : operands) {
        SCEV operand = intern(rawOperand);
        if (operand instanceof SCEV.Constant value) constant = constant.multiply(value.value());
        else nonConstants.add(operand);
      }
      constant = normalizeSigned(constant, type);
      if (constant.equals(ZERO)) return getConstant(type, ZERO);
      if (!constant.equals(ONE)) nonConstants.add(getConstant(type, constant));
      if (nonConstants.isEmpty()) return getConstant(type, ONE);
      if (nonConstants.size() == 1) return nonConstants.get(0);

      SCEV foldedRecurrence = tryFoldScaledAddRec(nonConstants, type);
      if (foldedRecurrence != null) return foldedRecurrence;
      nonConstants.sort(this::compareExpressions);
      return intern(new SCEV.Multiply(type, nonConstants));
    }

    private SCEV tryFoldScaledAddRec(List<SCEV> operands, Type type) {
      SCEV.AddRec recurrence = null;
      List<SCEV> invariants = new ArrayList<>();
      for (SCEV operand : operands) {
        if (operand instanceof SCEV.AddRec addRec) {
          if (recurrence != null) return null;
          recurrence = addRec;
        } else {
          invariants.add(operand);
        }
      }
      if (recurrence == null) return null;
      LoopAnalysis.Loop recurrenceLoop = recurrence.loop();
      if (invariants.stream().anyMatch(value -> !isLoopInvariant(value, recurrenceLoop))) {
        return null;
      }
      SCEV scale = invariants.size() == 1 ? invariants.get(0) : getMulExpr(invariants);
      return getAddRec(
          type,
          getMulExpr(recurrence.start(), scale),
          getMulExpr(recurrence.step(), scale),
          recurrence.loop());
    }

    private SCEV getSignedDivide(SCEV dividend, SCEV divisor) {
      if (dividend.getType() != divisor.getType()) {
        throw new IllegalArgumentException("SCEV divide type mismatch");
      }
      if (divisor instanceof SCEV.Constant right && right.value().equals(ONE)) return dividend;
      if (dividend instanceof SCEV.Constant left && divisor instanceof SCEV.Constant right) {
        BigInteger signedMinimum = ONE.shiftLeft(bitWidth(dividend.getType()) - 1).negate();
        boolean signedOverflow =
            left.value().equals(signedMinimum) && right.value().equals(NEGATIVE_ONE);
        if (!right.value().equals(ZERO) && !signedOverflow) {
          return getConstant(dividend.getType(), left.value().divide(right.value()));
        }
      }
      return intern(new SCEV.SignedDivide(dividend.getType(), dividend, divisor));
    }

    private SCEV getZeroExtend(SCEV operand, Type destinationType) {
      if (operand.getType() == destinationType) return operand;
      if (operand instanceof SCEV.Constant constant) {
        return getConstant(destinationType, toUnsigned(constant.value(), operand.getType()));
      }
      SCEV extendedRecurrence = tryExtendAddRec(operand, destinationType, false);
      if (extendedRecurrence != null) return extendedRecurrence;
      return intern(new SCEV.ZeroExtend(destinationType, operand));
    }

    private SCEV getSignExtend(SCEV operand, Type destinationType) {
      if (operand.getType() == destinationType) return operand;
      if (operand instanceof SCEV.Constant constant) {
        return getConstant(destinationType, constant.value());
      }
      SCEV extendedRecurrence = tryExtendAddRec(operand, destinationType, true);
      if (extendedRecurrence != null) return extendedRecurrence;
      return intern(new SCEV.SignExtend(destinationType, operand));
    }

    private SCEV tryExtendAddRec(SCEV operand, Type destinationType, boolean signed) {
      if (!(operand instanceof SCEV.AddRec recurrence)
          || !(recurrence.start() instanceof SCEV.Constant start)
          || !(recurrence.step() instanceof SCEV.Constant step)) {
        return null;
      }
      Optional<BigInteger> count = getConstantBackedgeTakenCount(recurrence.loop());
      if (count.isEmpty()) return null;

      BigInteger extendedStart =
          signed ? start.value() : toUnsigned(start.value(), operand.getType());
      BigInteger extendedStep = step.value();
      BigInteger finalValue = extendedStart.add(extendedStep.multiply(count.get()));
      if (!isRepresentableWithoutWrapping(finalValue, operand.getType(), !signed)
          || !isRepresentableWithoutWrapping(extendedStart, operand.getType(), !signed)) {
        return null;
      }
      return getAddRec(
          destinationType,
          getConstant(destinationType, extendedStart),
          getConstant(destinationType, extendedStep),
          recurrence.loop());
    }

    private SCEV getAddRec(
        Type type, SCEV start, SCEV step, LoopAnalysis.Loop loop) {
      if (isZero(step)) return start;
      return intern(new SCEV.AddRec(type, start, step, loop));
    }

    private SCEV.Unknown getUnknown(Value value) {
      return intern(new SCEV.Unknown(value));
    }

    private static boolean isValueLoopInvariant(Value value, LoopAnalysis.Loop loop) {
      return !(value instanceof Instruction instruction)
          || instruction.getParent() == null
          || !loop.contains(instruction.getParent());
    }

    private static Set<BasicBlock> getExitingBlocks(LoopAnalysis.Loop loop) {
      Set<BasicBlock> exitingBlocks = new LinkedHashSet<>();
      for (BasicBlock block : loop.blocks()) {
        if (block.getSuccessors().stream().anyMatch(successor -> !loop.contains(successor))) {
          exitingBlocks.add(block);
        }
      }
      return exitingBlocks;
    }

    @SuppressWarnings("unchecked")
    private <T extends SCEV> T intern(T expression) {
      T canonical = (T) uniqueExpressions.computeIfAbsent(expression, ignored -> expression);
      expressionIds.computeIfAbsent(canonical, ignored -> nextExpressionId++);
      return canonical;
    }

    private int compareExpressions(SCEV left, SCEV right) {
      if (left == right) return 0;
      int kindOrder = left.getClass().getName().compareTo(right.getClass().getName());
      if (kindOrder != 0) return kindOrder;
      return Integer.compare(expressionIds.get(left), expressionIds.get(right));
    }

    private static boolean isZero(SCEV expression) {
      return expression instanceof SCEV.Constant constant && constant.value().equals(ZERO);
    }

    private static Optional<BigInteger> solveExitCount(
        BigInteger signedStart,
        BigInteger signedStep,
        BigInteger signedBound,
        String predicate,
        Type type) {
      boolean unsigned = predicate.startsWith("u");
      BigInteger start = unsigned ? toUnsigned(signedStart, type) : signedStart;
      BigInteger step = signedStep;
      BigInteger bound = unsigned ? toUnsigned(signedBound, type) : signedBound;
      if (!evaluatePredicate(start, bound, predicate)) return Optional.of(ZERO);

      BigInteger count;
      switch (predicate) {
        case "slt", "ult" -> {
          if (step.signum() <= 0) return Optional.empty();
          count = ceilDivide(bound.subtract(start), step);
        }
        case "sle", "ule" -> {
          if (step.signum() <= 0) return Optional.empty();
          count = bound.subtract(start).divide(step).add(ONE);
        }
        case "sgt", "ugt" -> {
          if (step.signum() >= 0) return Optional.empty();
          count = ceilDivide(start.subtract(bound), step.negate());
        }
        case "sge", "uge" -> {
          if (step.signum() >= 0) return Optional.empty();
          count = start.subtract(bound).divide(step.negate()).add(ONE);
        }
        case "ne" -> {
          if (step.equals(ZERO)) return Optional.empty();
          BigInteger[] quotient = bound.subtract(start).divideAndRemainder(step);
          if (!quotient[1].equals(ZERO) || quotient[0].signum() < 0) return Optional.empty();
          count = quotient[0];
        }
        case "eq" -> {
          if (step.equals(ZERO)) return Optional.empty();
          count = ONE;
        }
        default -> {
          return Optional.empty();
        }
      }

      BigInteger exitValue = start.add(step.multiply(count));
      if (!isRepresentableWithoutWrapping(exitValue, type, unsigned)
          || evaluatePredicate(exitValue, bound, predicate)) {
        return Optional.empty();
      }
      return Optional.of(count);
    }

    private static boolean evaluatePredicate(BigInteger left, BigInteger right, String predicate) {
      int comparison = left.compareTo(right);
      return switch (predicate) {
        case "eq" -> comparison == 0;
        case "ne" -> comparison != 0;
        case "slt", "ult" -> comparison < 0;
        case "sle", "ule" -> comparison <= 0;
        case "sgt", "ugt" -> comparison > 0;
        case "sge", "uge" -> comparison >= 0;
        default -> false;
      };
    }

    private static String invertPredicate(String predicate) {
      if (predicate == null) return null;
      return switch (predicate) {
        case "eq" -> "ne";
        case "ne" -> "eq";
        case "slt" -> "sge";
        case "sle" -> "sgt";
        case "sgt" -> "sle";
        case "sge" -> "slt";
        case "ult" -> "uge";
        case "ule" -> "ugt";
        case "ugt" -> "ule";
        case "uge" -> "ult";
        default -> null;
      };
    }

    private static String swapPredicate(String predicate) {
      if (predicate == null) return null;
      return switch (predicate) {
        case "eq", "ne" -> predicate;
        case "slt" -> "sgt";
        case "sle" -> "sge";
        case "sgt" -> "slt";
        case "sge" -> "sle";
        case "ult" -> "ugt";
        case "ule" -> "uge";
        case "ugt" -> "ult";
        case "uge" -> "ule";
        default -> null;
      };
    }

    private static BigInteger ceilDivide(BigInteger numerator, BigInteger denominator) {
      return numerator.add(denominator).subtract(ONE).divide(denominator);
    }

    private static boolean isRepresentableWithoutWrapping(
        BigInteger value, Type type, boolean unsigned) {
      int width = bitWidth(type);
      if (width == 0) return false;
      if (unsigned) return value.signum() >= 0 && value.bitLength() <= width;
      BigInteger limit = ONE.shiftLeft(width - 1);
      return value.compareTo(limit.negate()) >= 0 && value.compareTo(limit) < 0;
    }

    private static BigInteger normalizeSigned(BigInteger value, Type type) {
      int width = bitWidth(type);
      if (width == 0) return value;
      BigInteger modulus = ONE.shiftLeft(width);
      BigInteger normalized = value.mod(modulus);
      BigInteger signBit = ONE.shiftLeft(width - 1);
      return normalized.compareTo(signBit) >= 0 ? normalized.subtract(modulus) : normalized;
    }

    private static BigInteger toUnsigned(BigInteger value, Type type) {
      int width = bitWidth(type);
      return width == 0 ? value : value.mod(ONE.shiftLeft(width));
    }

    private static int bitWidth(Type type) {
      return switch (type.dataType) {
        case I1 -> 1;
        case INT -> 32;
        case I64, POINTER -> 64;
        default -> 0;
      };
    }

    private static boolean isSCEVable(Type type) {
      return bitWidth(type) != 0;
    }

    private static BigInteger allocationSize(Type type) {
      return switch (type.dataType) {
        case I1 -> ONE;
        case INT, FLOAT -> BigInteger.valueOf(4);
        case I64, POINTER -> BigInteger.valueOf(8);
        case ARRAY -> BigInteger.valueOf(type.size).multiply(allocationSize(type.innerType));
        case VOID -> throw new IllegalArgumentException("void has no allocation size");
      };
    }

  }

  @Override
  public Result run(Function function, FunctionAnalysisManager fam) {
    FunctionAnalysisManager analyses = fam;
    if (analyses == null) {
      analyses = new FunctionAnalysisManager();
      analyses.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
      analyses.registerPass(LoopAnalysis.class, new LoopAnalysis());
    }
    DominatorTreeAnalysis.Result dominators =
        analyses.getResult(DominatorTreeAnalysis.class, function);
    LoopAnalysis.Result loops = analyses.getResult(LoopAnalysis.class, function);
    return new Result(function, loops, dominators);
  }
}
