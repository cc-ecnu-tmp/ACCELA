package accela.pass.ir.transform.affine;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.analysis.scev.SCEV;
import accela.pass.ir.analysis.scev.SCEVExpander;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/** Builds a guarded exact-i32 closed form for one matched extended affine loop. */
public final class ExtendedAffineTransform {
  private ExtendedAffineTransform() {}

  public static void apply(
      Function function,
      ExtendedAffineMatcher.Plan plan,
      ExtendedAffineProfitability.Assessment profitability,
      ScalarEvolutionAnalysis.Result scalarEvolution) {
    Objects.requireNonNull(function, "function");
    Objects.requireNonNull(plan, "plan");
    Objects.requireNonNull(profitability, "profitability");
    Objects.requireNonNull(scalarEvolution, "scalarEvolution");
    if (!profitability.profitable()) {
      throw new IllegalArgumentException("cannot apply an unprofitable extended affine plan");
    }

    BasicBlock header = plan.loop().header();
    BasicBlock summary = function.insertBlockAfter(
        header, header.getLabel() + ".extended.affine.summary");
    BasicBlock count = function.insertBlockAfter(
        header, header.getLabel() + ".extended.affine.count");
    BasicBlock entry = count;

    if (plan.inductionStep() > 1) {
      BasicBlock range = function.insertBlockAfter(
          header, header.getLabel() + ".extended.affine.range");
      IRBuilder rangeBuilder = new IRBuilder(range);
      int maximumBound = Integer.MAX_VALUE - (plan.inductionStep() - 1);
      Value safeBound = rangeBuilder.createICmp(
          "sle", plan.bound(), Constant.intConst(maximumBound));
      rangeBuilder.createCondBr(safeBound, count, plan.body());
      entry = range;
    }

    IRBuilder countBuilder = new IRBuilder(count);
    Value tripCount = createTripCount(countBuilder, plan);
    Value currentIteration = createCurrentIteration(countBuilder, plan);
    Value enoughIterations = countBuilder.createICmp(
        "sge", tripCount, Constant.intConst(profitability.minimumTripCount()));
    countBuilder.createCondBr(enoughIterations, summary, plan.body());

    IRBuilder summaryBuilder = new IRBuilder(summary);
    Value chooseTwo = null;
    Value chooseThree = null;
    if (plan.maximumDeltaDegree() >= 1) {
      chooseTwo = createChooseTwo(summaryBuilder, tripCount);
    }
    if (plan.maximumDeltaDegree() >= 2) {
      chooseThree = createChooseThree(summaryBuilder, tripCount);
    }
    Value squareSum = chooseThree == null ? null : summaryBuilder.createAdd(
        summaryBuilder.createMul(Constant.intConst(2), chooseThree), chooseTwo);

    SCEVExpander expander = new SCEVExpander(scalarEvolution, plan.loop(), summaryBuilder);
    List<Value> finalStates = new ArrayList<>();
    for (ExtendedAffineMatcher.StateRecurrence recurrence : plan.recurrences()) {
      List<SCEV> coefficients = recurrence.delta().coefficients();
      Value constant = expander.expandInvariantInteger(coefficients.getFirst());
      Value linear = coefficients.size() >= 2
          ? expander.expandInvariantInteger(coefficients.get(1)) : Constant.intConst(0);
      Value quadratic = coefficients.size() >= 3
          ? expander.expandInvariantInteger(coefficients.get(2)) : Constant.intConst(0);

      Value localConstant = constant;
      Value localLinear = linear;
      if (coefficients.size() >= 2) {
        localConstant = summaryBuilder.createAdd(
            localConstant, summaryBuilder.createMul(linear, currentIteration));
      }
      if (coefficients.size() >= 3) {
        Value iterationSquared = summaryBuilder.createMul(currentIteration, currentIteration);
        localConstant = summaryBuilder.createAdd(
            localConstant, summaryBuilder.createMul(quadratic, iterationSquared));
        localLinear = summaryBuilder.createAdd(
            linear,
            summaryBuilder.createMul(
                summaryBuilder.createMul(Constant.intConst(2), quadratic), currentIteration));
      }

      Value accumulated = summaryBuilder.createMul(localConstant, tripCount);
      if (coefficients.size() >= 2) {
        accumulated = summaryBuilder.createAdd(
            accumulated, summaryBuilder.createMul(localLinear, chooseTwo));
      }
      if (coefficients.size() >= 3) {
        accumulated = summaryBuilder.createAdd(
            accumulated, summaryBuilder.createMul(quadratic, squareSum));
      }
      finalStates.add(summaryBuilder.createAdd(recurrence.phi(), accumulated));
    }

    Value finalInduction = plan.inductionStep() == 1
        ? plan.bound()
        : summaryBuilder.createAdd(
            plan.induction(),
            summaryBuilder.createMul(
                tripCount, Constant.intConst(plan.inductionStep())));
    summaryBuilder.createBr(header);

    plan.headerBranch().setOperand(plan.insideSuccessorOperand(), entry);
    plan.induction().addOperand(finalInduction);
    plan.induction().addOperand(summary);
    for (int index = 0; index < plan.recurrences().size(); index++) {
      plan.recurrences().get(index).phi().addOperand(finalStates.get(index));
      plan.recurrences().get(index).phi().addOperand(summary);
    }
  }

  private static Value createTripCount(
      IRBuilder builder, ExtendedAffineMatcher.Plan plan) {
    Value difference = builder.createSub(plan.bound(), plan.induction());
    if (plan.inductionStep() == 1) return difference;
    Value adjusted = builder.createSub(difference, Constant.intConst(1));
    Value quotient = builder.createSDiv(adjusted, Constant.intConst(plan.inductionStep()));
    return builder.createAdd(quotient, Constant.intConst(1));
  }

  private static Value createCurrentIteration(
      IRBuilder builder, ExtendedAffineMatcher.Plan plan) {
    Value distance = plan.inductionStart() == 0
        ? plan.induction()
        : builder.createSub(plan.induction(), Constant.intConst(plan.inductionStart()));
    return plan.inductionStep() == 1
        ? distance
        : builder.createSDiv(distance, Constant.intConst(plan.inductionStep()));
  }

  /** Exact C(n, 2): cancel the even factor before the only potentially wrapping multiply. */
  private static Value createChooseTwo(IRBuilder builder, Value count) {
    Value parity = builder.createAnd(count, Constant.intConst(1));
    Value even = builder.createSub(count, parity);
    Value odd = builder.createAdd(
        builder.createSub(count, Constant.intConst(1)), parity);
    Value halfEven = builder.createSDiv(even, Constant.intConst(2));
    return builder.createMul(halfEven, odd);
  }

  /**
   * Exact C(n, 3): cancel one factor of three and one factor of two before multiplication.
   * The caller reaches this block only when the profitability guard proves n >= 4, so n-1 and
   * n-2 are nonnegative and every division has a fixed positive divisor.
   */
  private static Value createChooseThree(IRBuilder builder, Value count) {
    Value one = Constant.intConst(1);
    Value minusOne = builder.createSub(count, one);
    Value minusTwo = builder.createSub(count, Constant.intConst(2));
    Value remainder = builder.createSRem(count, Constant.intConst(3));
    Value isZero = builder.createZExt(
        builder.createICmp("eq", remainder, Constant.intConst(0)), Type.INT);
    Value isOne = builder.createZExt(
        builder.createICmp("eq", remainder, Constant.intConst(1)), Type.INT);
    Value isTwo = builder.createZExt(
        builder.createICmp("eq", remainder, Constant.intConst(2)), Type.INT);

    Value factor0 = choose(
        builder, isZero, builder.createSDiv(count, Constant.intConst(3)), count);
    Value factor1 = choose(
        builder, isOne, builder.createSDiv(minusOne, Constant.intConst(3)), minusOne);
    Value factor2 = choose(
        builder, isTwo, builder.createSDiv(minusTwo, Constant.intConst(3)), minusTwo);

    Value parity = builder.createAnd(count, one);
    Value evenCount = builder.createSub(one, parity);
    factor0 = choose(
        builder, evenCount, builder.createSDiv(factor0, Constant.intConst(2)), factor0);
    factor1 = choose(
        builder, parity, builder.createSDiv(factor1, Constant.intConst(2)), factor1);
    return builder.createMul(builder.createMul(factor0, factor1), factor2);
  }

  private static Value choose(
      IRBuilder builder, Value condition, Value whenTrue, Value whenFalse) {
    Value inverse = builder.createSub(Constant.intConst(1), condition);
    return builder.createAdd(
        builder.createMul(condition, whenTrue),
        builder.createMul(inverse, whenFalse));
  }
}
