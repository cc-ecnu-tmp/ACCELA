package accela.ir;

/** Exact two's-complement i32 operations for compile-time IR reasoning. */
public final class ExactI32 {
  private ExactI32() {}

  /** Normalizes any stored integer payload to the low 32-bit IR value. */
  public static int normalize(long value) {
    return (int) value;
  }

  public static int add(int left, int right) {
    return left + right;
  }

  public static int subtract(int left, int right) {
    return left - right;
  }

  public static int multiply(int left, int right) {
    return left * right;
  }

  /** Returns the signed high half of the exact i32-by-i32 product. */
  public static int multiplyHigh(int left, int right) {
    return (int) (((long) left * (long) right) >> Integer.SIZE);
  }

  /** Signed division for source-defined operands. Division by zero is deliberately rejected. */
  public static int divide(int dividend, int divisor) {
    if (divisor == 0) throw new ArithmeticException("i32 division by zero");
    return dividend / divisor;
  }

  /** Signed remainder for source-defined operands. Division by zero is deliberately rejected. */
  public static int remainder(int dividend, int divisor) {
    if (divisor == 0) throw new ArithmeticException("i32 remainder by zero");
    return dividend % divisor;
  }

  public static int shiftLeft(int value, int amount) {
    requireShiftAmount(amount);
    return value << amount;
  }

  public static int arithmeticShiftRight(int value, int amount) {
    requireShiftAmount(amount);
    return value >> amount;
  }

  public static int and(int left, int right) {
    return left & right;
  }

  public static int xor(int left, int right) {
    return left ^ right;
  }

  public static boolean compare(String predicate, int left, int right) {
    return switch (predicate) {
      case "eq" -> left == right;
      case "ne" -> left != right;
      case "slt" -> left < right;
      case "sle" -> left <= right;
      case "sgt" -> left > right;
      case "sge" -> left >= right;
      default -> throw new IllegalArgumentException("unsupported i32 predicate: " + predicate);
    };
  }

  private static void requireShiftAmount(int amount) {
    if (amount < 0 || amount >= Integer.SIZE) {
      throw new ArithmeticException("i32 shift amount outside 0..31: " + amount);
    }
  }
}
