package accela.backend.target;

/** Granlund-Montgomery multiplier for positive signed 32-bit divisors. */
record SignedDivisionMagic(int postShift, long multiplier) {
  static SignedDivisionMagic forDivisor(int divisor) {
    if (divisor <= 1 || divisor > (1 << 30)) {
      throw new IllegalArgumentException("divisor out of supported range");
    }
    int leadingBits = 32 - Integer.numberOfLeadingZeros(divisor - 1);
    int postShift = leadingBits;
    long numerator = 1L << (32 + leadingBits);
    long low = numerator / divisor;
    long high = (numerator + (1L << (leadingBits + 1))) / divisor;
    while (low / 2 < high / 2 && postShift > 0) {
      low /= 2;
      high /= 2;
      postShift--;
    }
    return new SignedDivisionMagic(postShift, high);
  }
}
