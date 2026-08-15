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
import java.math.BigInteger;
import java.util.ArrayList;

/** Lowers signed i32 division and remainder by constants to multiplication. */
public final class StrengthReduction {
  private static final long SIGN_BIT = 1L << (Integer.SIZE - 1);
  private static final int DIVISION_PRECISION = Integer.SIZE - 1;

  private StrengthReduction() {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }

  public static boolean runOnFunction(Function function) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
        if (rewrite(instruction)) changed = true;
      }
    }
    return changed;
  }

  private static boolean rewrite(Instruction instruction) {
    Instruction.Opcode opcode = instruction.getOpcode();
    if (opcode != Instruction.Opcode.SDIV && opcode != Instruction.Opcode.SREM) return false;
    Integer constant = uniformI32Constant(instruction.getOperand(1));
    Type type = instruction.getType();
    if (constant == null
        || !(type == Type.INT || type.isVector() && type.getElementType() == Type.INT)) return false;

    int divisor = constant;
    if (divisor == 0) return false;
    long magnitude = Math.abs((long) divisor);

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(instruction);
    Value dividend = instruction.getOperand(0);
    Value replacement;
    if (opcode == Instruction.Opcode.SREM && magnitude == 1) {
      replacement = type.isVector() ? Constant.zero(type) : Constant.intConst(0);
    } else if (divisor == 1) {
      replacement = dividend;
    } else if (divisor == -1) {
      replacement = builder.createSub(Constant.intConst(0), dividend);
    } else if (isPowerOfTwo(magnitude)) {
      return false; // The late RISC-V selector has a lower-pressure sequence for this case.
    } else {
      Value quotient = buildQuotient(builder, dividend, divisor);
      replacement = opcode == Instruction.Opcode.SDIV
          ? quotient
          : builder.createSub(
              dividend, builder.createMul(quotient, Constant.intConst(divisor)));
    }
    instruction.replaceAllUsesWith(replacement);
    instruction.eraseFromParent();
    return true;
  }

  /** Returns a uniform scalar/vector i32 constant, or {@code null}. */
  private static Integer uniformI32Constant(Value value) {
    if (value instanceof Constant.Int integer && integer.getType() == Type.INT)
      return (int) integer.value;
    if (value instanceof Constant.Zero zero
        && zero.getType().isVector()
        && zero.getType().getElementType() == Type.INT) return 0;
    if (!(value instanceof Constant.Vector vector)
        || vector.getType().getElementType() != Type.INT
        || vector.elements.isEmpty()
        || !(vector.elements.getFirst() instanceof Constant.Int first)) return null;
    for (Constant element : vector.elements) {
      if (!(element instanceof Constant.Int integer) || integer.value != first.value) return null;
    }
    return (int) first.value;
  }

  private static Value buildQuotient(IRBuilder builder, Value dividend, int divisor) {
    Magic magic = chooseSignedDivisionMagic(divisor);
    boolean addDividend = magic.multiplier() >= SIGN_BIT;

    // The paper's unsigned 32-bit multiplier is consumed as the same i32 bit pattern.
    Value quotient =
        builder.createSMulH(dividend, Constant.intConst((int) magic.multiplier()));
    if (addDividend) quotient = builder.createAdd(quotient, dividend);
    if (magic.shift() != 0) {
      quotient = builder.createAShr(quotient, Constant.intConst(magic.shift()));
    }
    Value sign = builder.createAShr(dividend, Constant.intConst(Integer.SIZE - 1));
    quotient = builder.createSub(quotient, sign);
    return divisor < 0 ? builder.createSub(Constant.intConst(0), quotient) : quotient;
  }

  private static boolean isPowerOfTwo(long value) {
    return value > 0 && (value & (value - 1)) == 0;
  }

  /** Selects the multiplier and shift from Granlund and Montgomery, Figure 6.2. */
  static Magic chooseSignedDivisionMagic(int divisor) {
    long absolute = Math.abs((long) divisor);
    if (absolute < 2) throw new IllegalArgumentException("trivial divisor: " + divisor);

    int shift = Long.SIZE - Long.numberOfLeadingZeros(absolute - 1);
    BigInteger d = BigInteger.valueOf(absolute);
    BigInteger numerator = BigInteger.ONE.shiftLeft(Integer.SIZE + shift);
    BigInteger low = numerator.divide(d);
    BigInteger high = numerator
        .add(BigInteger.ONE.shiftLeft(Integer.SIZE + shift - DIVISION_PRECISION))
        .divide(d);

    while (shift > 0 && low.shiftRight(1).compareTo(high.shiftRight(1)) < 0) {
      low = low.shiftRight(1);
      high = high.shiftRight(1);
      shift--;
    }
    return new Magic(high.longValueExact(), shift);
  }

  record Magic(long multiplier, int shift) {}
}
