package accela.backend.target;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

final class SignedDivisionMagicTest {
  @Test
  void reproducesTruncatingSignedDivision() {
    int[] divisors = {3, 5, 7, 11, 1000};
    int[] edgeCases = {Integer.MIN_VALUE, -10001, -1, 0, 1, 10001, Integer.MAX_VALUE};
    for (int divisor : divisors) {
      SignedDivisionMagic magic = SignedDivisionMagic.forDivisor(divisor);
      for (int numerator : edgeCases) {
        assertEquals(numerator / divisor, divide(numerator, magic));
      }
      for (int numerator = -10000; numerator <= 10000; numerator++) {
        assertEquals(numerator / divisor, divide(numerator, magic));
      }
    }
  }

  private static int divide(int numerator, SignedDivisionMagic magic) {
    long value = numerator;
    long quotient;
    if (magic.multiplier() < (1L << 31)) {
      quotient = (value * magic.multiplier()) >> (32 + magic.postShift());
    } else {
      quotient = (value * (magic.multiplier() - (1L << 32))) >> 32;
      quotient += value;
      quotient >>= magic.postShift();
    }
    return (int) (quotient - (value >> 31));
  }
}
