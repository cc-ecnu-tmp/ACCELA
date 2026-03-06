package accela.ir;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * Compile-time constant values: integer/float literals, zeroinitializer,
 * and array constants (for global initializers).
 */
public abstract class Constant extends Value {

  protected Constant(Type type, String name) {
    super(type, name);
  }

  public static Int intConst(long value) {
    return new Int(value);
  }

  public static Int int64Const(long value) {
    return new Int(Type.I64, value);
  }

  public static Int boolConst(boolean value) {
    return new Int(Type.I1, value ? 1 : 0);
  }

  public static Float floatConst(float value) {
    return new Float(value);
  }

  public static Zero zero(Type type) {
    return new Zero(type);
  }

  public static Array array(Type type, List<Constant> elements) {
    return new Array(type, elements);
  }

  /** An integer constant (i1, i32, or i64). */
  public static class Int extends Constant {
    public final long value;

    Int(long value) {
      super(Type.INT, String.valueOf((int) value));
      this.value = value;
    }

    Int(Type type, long value) {
      super(type, formatInt(type, value));
      this.value = value;
    }

    private static String formatInt(Type type, long value) {
      if (type == Type.I1) return value != 0 ? "true" : "false";
      return String.valueOf(value);
    }
  }

  /** A float constant. */
  public static class Float extends Constant {
    public final float value;

    Float(float value) {
      super(Type.FLOAT, floatLiteral(value));
      this.value = value;
    }

    public static String floatLiteral(float f) {
      if (java.lang.Float.floatToRawIntBits(f) == 0) return "0.000000e+00";
      if (java.lang.Float.floatToRawIntBits(f) == 0x80000000) return "-0.000000e+00";
      long bits = Double.doubleToRawLongBits((double) f);
      return String.format("0x%016X", bits);
    }
  }

  public static class Zero extends Constant {
    Zero(Type type) {
      super(type, "zeroinitializer");
    }
  }

  /** A constant array: [N x T] [T val0, T val1, ...]. */
  public static class Array extends Constant {
    public final List<Constant> elements;

    Array(Type type, List<Constant> elements) {
      super(type, null); // name computed by printer
      this.elements = Collections.unmodifiableList(elements);
    }
  }
}
