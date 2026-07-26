package accela.pass.ir.transform;

import java.math.BigInteger;

/**
 * Compile-time multiplier selection from Granlund and Montgomery, Figure 6.2.
 *
 * <p>The precision is 31 because ACCELA divides signed 32-bit integers.
 */
final class SignedDivisionMagic {
  private static final int BITS = Integer.SIZE;
  private static final int PRECISION = BITS - 1;

  private SignedDivisionMagic() {}

  static Magic choose(int divisor) {
    long absolute = Math.abs((long) divisor);
    if (absolute < 2) throw new IllegalArgumentException("trivial divisor: " + divisor);

    int log = Long.SIZE - Long.numberOfLeadingZeros(absolute - 1);
    int shift = log;
    BigInteger d = BigInteger.valueOf(absolute);
    BigInteger numerator = BigInteger.ONE.shiftLeft(BITS + log);
    BigInteger low = numerator.divide(d);
    BigInteger high =
        numerator.add(BigInteger.ONE.shiftLeft(BITS + log - PRECISION)).divide(d);

    while (shift > 0 && low.shiftRight(1).compareTo(high.shiftRight(1)) < 0) {
      low = low.shiftRight(1);
      high = high.shiftRight(1);
      shift--;
    }
    return new Magic(high.longValueExact(), shift);
  }

  record Magic(long multiplier, int shift) {}
}
