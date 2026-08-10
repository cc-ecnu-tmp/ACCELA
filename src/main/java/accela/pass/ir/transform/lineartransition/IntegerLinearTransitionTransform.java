package accela.pass.ir.transform.lineartransition;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.List;

/** Rewrites one legal transition loop to guarded in-function matrix binary lifting. */
final class IntegerLinearTransitionTransform {
  private IntegerLinearTransitionTransform() {}

  static void apply(
      Function function,
      IntegerLinearTransitionMatcher.Candidate candidate,
      IntegerLinearTransitionProfitability.Plan plan) {
    BasicBlock header = candidate.loop().header();
    String prefix = header.getLabel() + ".linear.transition";
    BasicBlock prepare = function.insertBlockAfter(header, prefix + ".prepare");
    BasicBlock liftingHeader = function.insertBlockAfter(prepare, prefix + ".header");
    BasicBlock testBit = function.insertBlockAfter(liftingHeader, prefix + ".testbit");
    BasicBlock applyBit = function.insertBlockAfter(testBit, prefix + ".apply");
    BasicBlock square = function.insertBlockAfter(applyBit, prefix + ".square");
    BasicBlock done = function.insertBlockAfter(square, prefix + ".done");

    IRBuilder prepareBuilder = new IRBuilder(prepare);
    // This block is reached only from the original signed `induction < bound` body edge.
    // The matcher proves induction starts nonnegative and increments by one, so this i32
    // subtraction is in [1, INT_MAX] and cannot wrap.
    Value tripCount = prepareBuilder.createSub(candidate.bound(), candidate.induction());
    Value profitable = prepareBuilder.createICmp(
        "sge", tripCount, Constant.intConst(plan.runtimeTripThreshold()));
    prepareBuilder.createCondBr(profitable, liftingHeader, candidate.body());

    IRBuilder headerBuilder = new IRBuilder(liftingHeader);
    Instruction remaining = headerBuilder.createPhi(Type.INT);
    remaining.addOperand(tripCount);
    remaining.addOperand(prepare);

    List<Instruction> result = new ArrayList<>();
    for (Instruction state : candidate.states()) {
      Instruction value = headerBuilder.createPhi(Type.INT);
      value.addOperand(state);
      value.addOperand(prepare);
      result.add(value);
    }

    int matrixDimension = candidate.matrixDimension();
    Instruction[][] base = new Instruction[matrixDimension][matrixDimension];
    int[][] initialMatrix = candidate.homogeneousTransition();
    for (int row = 0; row < matrixDimension; row++) {
      for (int column = 0; column < matrixDimension; column++) {
        Instruction value = headerBuilder.createPhi(Type.INT);
        value.addOperand(Constant.intConst(initialMatrix[row][column]));
        value.addOperand(prepare);
        base[row][column] = value;
      }
    }
    Value hasWork = headerBuilder.createICmp("ne", remaining, Constant.intConst(0));
    headerBuilder.createCondBr(hasWork, testBit, done);

    IRBuilder bitBuilder = new IRBuilder(testBit);
    Value lowBit = bitBuilder.createAnd(remaining, Constant.intConst(1));
    Value isOdd = bitBuilder.createICmp("ne", lowBit, Constant.intConst(0));
    bitBuilder.createCondBr(isOdd, applyBit, square);

    IRBuilder applyBuilder = new IRBuilder(applyBit);
    List<Value> homogeneousResult = new ArrayList<>(result);
    homogeneousResult.add(Constant.intConst(1));
    List<Value> appliedResult = new ArrayList<>();
    for (int row = 0; row < candidate.stateDimension(); row++) {
      appliedResult.add(dotProduct(applyBuilder, base[row], homogeneousResult));
    }
    applyBuilder.createBr(square);

    IRBuilder squareBuilder = new IRBuilder(square);
    List<Instruction> mergedResult = new ArrayList<>();
    for (int index = 0; index < candidate.stateDimension(); index++) {
      Instruction merged = squareBuilder.createPhi(Type.INT);
      merged.addOperand(result.get(index));
      merged.addOperand(testBit);
      merged.addOperand(appliedResult.get(index));
      merged.addOperand(applyBit);
      mergedResult.add(merged);
    }
    Value[][] squaredBase = new Value[matrixDimension][matrixDimension];
    for (int row = 0; row < matrixDimension; row++) {
      for (int column = 0; column < matrixDimension; column++) {
        List<Value> left = new ArrayList<>();
        List<Value> right = new ArrayList<>();
        for (int inner = 0; inner < matrixDimension; inner++) {
          left.add(base[row][inner]);
          right.add(base[inner][column]);
        }
        squaredBase[row][column] = dotProduct(squareBuilder, left, right);
      }
    }
    Value nextRemaining = squareBuilder.createSDiv(remaining, Constant.intConst(2));
    squareBuilder.createBr(liftingHeader);

    remaining.addOperand(nextRemaining);
    remaining.addOperand(square);
    for (int index = 0; index < result.size(); index++) {
      result.get(index).addOperand(mergedResult.get(index));
      result.get(index).addOperand(square);
    }
    for (int row = 0; row < matrixDimension; row++) {
      for (int column = 0; column < matrixDimension; column++) {
        base[row][column].addOperand(squaredBase[row][column]);
        base[row][column].addOperand(square);
      }
    }

    new IRBuilder(done).createBr(header);
    candidate.induction().addOperand(candidate.bound());
    candidate.induction().addOperand(done);
    for (int index = 0; index < candidate.states().size(); index++) {
      candidate.states().get(index).addOperand(result.get(index));
      candidate.states().get(index).addOperand(done);
    }
    candidate.branch().setOperand(candidate.bodySuccessorOperand(), prepare);
  }

  private static Value dotProduct(IRBuilder builder, Value[] left, List<? extends Value> right) {
    return dotProduct(builder, List.of(left), right);
  }

  private static Value dotProduct(
      IRBuilder builder, List<? extends Value> left, List<? extends Value> right) {
    if (left.size() != right.size() || left.isEmpty()) {
      throw new IllegalArgumentException("dot-product dimensions must be equal and nonempty");
    }
    Value sum = null;
    for (int index = 0; index < left.size(); index++) {
      Value product = multiply(builder, left.get(index), right.get(index));
      sum = sum == null ? product : builder.createAdd(sum, product);
    }
    return sum;
  }

  private static Value multiply(IRBuilder builder, Value left, Value right) {
    if (isConstant(left, 0) || isConstant(right, 0)) return Constant.intConst(0);
    if (isConstant(left, 1)) return right;
    if (isConstant(right, 1)) return left;
    return builder.createMul(left, right);
  }

  private static boolean isConstant(Value value, int expected) {
    return value instanceof Constant.Int constant && (int) constant.value == expected;
  }
}
