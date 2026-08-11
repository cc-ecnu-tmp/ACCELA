package accela.pass.ir.analysis.scev;

import accela.ir.Constant;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Materializes loop-invariant i32 SCEVs at a caller-selected insertion point. */
public final class SCEVExpander {
  private final ScalarEvolutionAnalysis.Result scalarEvolution;
  private final LoopAnalysis.Loop loop;
  private final IRBuilder builder;
  private final Map<SCEV, Value> expanded = new IdentityHashMap<>();

  public SCEVExpander(
      ScalarEvolutionAnalysis.Result scalarEvolution,
      LoopAnalysis.Loop loop,
      IRBuilder builder) {
    this.scalarEvolution = Objects.requireNonNull(scalarEvolution, "scalarEvolution");
    this.loop = Objects.requireNonNull(loop, "loop");
    this.builder = Objects.requireNonNull(builder, "builder");
  }

  /**
   * Expands one proven loop-invariant i32 expression.
   *
   * <p>Recurrences and iteration polynomials are deliberately rejected: clients must first reduce
   * them to invariant coefficients and provide the iteration arithmetic explicitly.
   */
  public Value expandInvariantInteger(SCEV expression) {
    Objects.requireNonNull(expression, "expression");
    if (expression.getType() != Type.INT) {
      throw new IllegalArgumentException("SCEV expansion requires an i32 expression");
    }
    if (!scalarEvolution.isLoopInvariant(expression, loop)) {
      throw new IllegalArgumentException("SCEV expansion requires a loop-invariant expression");
    }
    return expanded.computeIfAbsent(expression, this::expand);
  }

  private Value expand(SCEV expression) {
    if (expression instanceof SCEV.Constant constant) {
      return Constant.intConst(constant.value().intValueExact());
    }
    if (expression instanceof SCEV.Unknown unknown) return unknown.value();
    if (expression instanceof SCEV.Add add) {
      return fold(add.operands(), true);
    }
    if (expression instanceof SCEV.Multiply multiply) {
      return fold(multiply.operands(), false);
    }
    if (expression instanceof SCEV.SignedDivide divide) {
      return builder.createSDiv(
          expandInvariantInteger(divide.dividend()),
          expandInvariantInteger(divide.divisor()));
    }
    if (expression instanceof SCEV.ZeroExtend extend) {
      return builder.createZExt(expandInvariant(extend.operand()), Type.INT);
    }
    if (expression instanceof SCEV.SignExtend extend) {
      return builder.createSExt(expandInvariant(extend.operand()), Type.INT);
    }
    throw new IllegalArgumentException(
        "unsupported invariant SCEV expansion: " + expression.getClass().getSimpleName());
  }

  private Value fold(List<SCEV> operands, boolean addition) {
    Value result = expandInvariantInteger(operands.getFirst());
    for (SCEV operand : operands.subList(1, operands.size())) {
      Value next = expandInvariantInteger(operand);
      result = addition ? builder.createAdd(result, next) : builder.createMul(result, next);
    }
    return result;
  }

  private Value expandInvariant(SCEV expression) {
    if (expression instanceof SCEV.Constant constant) {
      if (constant.getType() == Type.I1) {
        return Constant.boolConst(!constant.value().equals(java.math.BigInteger.ZERO));
      }
      throw new IllegalArgumentException("unsupported non-i32 SCEV constant expansion");
    }
    if (expression instanceof SCEV.Unknown unknown
        && scalarEvolution.isLoopInvariant(expression, loop)) {
      return unknown.value();
    }
    throw new IllegalArgumentException("unsupported non-i32 invariant SCEV expansion");
  }
}
