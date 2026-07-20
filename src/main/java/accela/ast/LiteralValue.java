package accela.ast;

/** Typed numeric payload for a SysY literal after the parser consumes its source spelling. */
public final class LiteralValue {
  public enum Kind {
    INT,
    FLOAT
  }

  private final Kind kind;
  private final int intValue;
  private final float floatValue;

  private LiteralValue(Kind kind, int intValue, float floatValue) {
    this.kind = kind;
    this.intValue = intValue;
    this.floatValue = floatValue;
  }

  public static LiteralValue ofInt(int value) {
    return new LiteralValue(Kind.INT, value, value);
  }

  public static LiteralValue ofFloat(float value) {
    return new LiteralValue(Kind.FLOAT, (int) value, value);
  }

  /**
   * Converts token text at the parse boundary into the language's 32-bit numeric representation.
   *
   * <p>SysY 2022 follows C integer and floating literal syntax but ignores suffixes. Hexadecimal
   * floating literals are distinguished by their required binary exponent ({@code p}/{@code P}); a
   * trailing hexadecimal {@code f} remains a digit rather than being mistaken for a suffix.
   */
  public static LiteralValue parse(String spelling) {
    boolean hexadecimal = spelling.startsWith("0x") || spelling.startsWith("0X");
    boolean floating =
        spelling.indexOf('.') >= 0
            || spelling.indexOf('p') >= 0
            || spelling.indexOf('P') >= 0
            || (!hexadecimal && (spelling.indexOf('e') >= 0 || spelling.indexOf('E') >= 0))
            || (!hexadecimal && (spelling.endsWith("f") || spelling.endsWith("F")));

    if (floating) {
      String value = spelling.replaceFirst("[fFlL]$", "");
      return ofFloat(Float.parseFloat(value));
    }

    String value = spelling.replaceFirst("[uUlL]+$", "");
    int radix = 10;
    int start = 0;
    if (value.startsWith("0x") || value.startsWith("0X")) {
      radix = 16;
      start = 2;
    } else if (value.length() > 1 && value.charAt(0) == '0') {
      radix = 8;
      start = 1;
    }
    return ofInt((int) Long.parseLong(value.substring(start), radix));
  }

  public Kind kind() {
    return kind;
  }

  public boolean isFloat() {
    return kind == Kind.FLOAT;
  }

  public int asInt() {
    return kind == Kind.INT ? intValue : (int) floatValue;
  }

  public float asFloat() {
    return kind == Kind.FLOAT ? floatValue : intValue;
  }

  public Number asNumber() {
    if (kind == Kind.FLOAT) return Float.valueOf(floatValue);
    return Integer.valueOf(intValue);
  }

  public boolean isZero() {
    return kind == Kind.FLOAT ? floatValue == 0.0f : intValue == 0;
  }

  public String debugText() {
    return kind == Kind.FLOAT ? Float.toString(floatValue) : Integer.toString(intValue);
  }
}
