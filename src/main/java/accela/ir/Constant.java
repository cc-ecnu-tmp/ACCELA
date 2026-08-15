package accela.ir;

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

  /** Creates a type-strict homogeneous vector constant. */
  public static Vector vector(Type type, List<Constant> elements) {
    return new Vector(type, elements);
  }

  /**
   * Creates a homogeneous vector constant while applying the IR normalization policy for mixed
   * numeric literals. Exactly integral float lanes such as {@code 1.0} normalize to integers;
   * otherwise, for example {@code {1, 2.5}}, integer lanes are promoted to float. The IR never
   * contains heterogeneous lane types.
   */
  public static Vector vector(List<Constant> elements) {
    if (elements.isEmpty()) throw new IllegalArgumentException("vector constant cannot be empty");
    Type elementType = commonVectorElementType(elements);
    List<Constant> converted = elements.stream()
        .map(element -> convertVectorElement(element, elementType))
        .toList();
    return new Vector(Type.vector(elementType, converted.size()), converted);
  }

  private static Type commonVectorElementType(List<Constant> elements) {
    boolean hasFloat = false;
    boolean hasI64 = false;
    boolean hasI32 = false;
    for (Constant element : elements) {
      Type type = element.getType();
      if (type == Type.FLOAT) {
        if (element instanceof Float floating && exactI32(floating.value) != null) hasI32 = true;
        else if (element instanceof Zero) hasI32 = true;
        else hasFloat = true;
      }
      else if (type == Type.I64) hasI64 = true;
      else if (type == Type.INT) hasI32 = true;
      else if (type != Type.I1) {
        throw new IllegalArgumentException("unsupported vector constant element type: " + type);
      }
      if (!(element instanceof Int) && !(element instanceof Float) && !(element instanceof Zero)) {
        throw new IllegalArgumentException("vector constants require scalar constant elements");
      }
    }
    if (hasFloat) return Type.FLOAT;
    if (hasI64) return Type.I64;
    if (hasI32) return Type.INT;
    return Type.I1;
  }

  private static Constant convertVectorElement(Constant element, Type destination) {
    if (element.getType().equals(destination)) return element;
    if (destination == Type.FLOAT) {
      if (element instanceof Int integer) return floatConst((float) integer.value);
      if (element instanceof Zero) return floatConst(0.0f);
    }
    if (destination == Type.I64 && element instanceof Int integer) {
      return int64Const(integer.value);
    }
    if (destination == Type.I64 && element instanceof Float floating) {
      java.lang.Integer value = exactI32(floating.value);
      if (value != null) return int64Const(value);
    }
    if (destination == Type.I64 && element instanceof Zero) return int64Const(0);
    if (destination == Type.INT && element instanceof Int integer) {
      return intConst(integer.value);
    }
    if (destination == Type.INT && element instanceof Float floating) {
      java.lang.Integer value = exactI32(floating.value);
      if (value != null) return intConst(value);
    }
    if (destination == Type.INT && element instanceof Zero) return intConst(0);
    throw new IllegalArgumentException(
        "cannot convert vector constant element from " + element.getType() + " to " + destination);
  }

  /** Returns an exactly represented i32 value, excluding negative zero, or {@code null}. */
  static java.lang.Integer exactI32(float value) {
    if (!java.lang.Float.isFinite(value)
        || java.lang.Float.floatToRawIntBits(value) == 0x80000000) return null;
    double widened = value;
    if (widened < java.lang.Integer.MIN_VALUE
        || widened > java.lang.Integer.MAX_VALUE
        || Math.rint(widened) != widened) return null;
    return (int) widened;
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
      this.elements = List.copyOf(elements);
    }
  }

  /** A fixed-width homogeneous vector constant. */
  public static class Vector extends Constant {
    public final List<Constant> elements;

    Vector(Type type, List<Constant> elements) {
      super(type, vectorName(elements));
      if (!type.isVector()) throw new IllegalArgumentException("vector constant requires vector type");
      if (elements.size() != type.getLaneCount()) {
        throw new IllegalArgumentException(
            "vector constant lane count mismatch: expected " + type.getLaneCount()
                + ", got " + elements.size());
      }
      for (Constant element : elements) {
        if (!element.getType().equals(type.getElementType())) {
          throw new IllegalArgumentException(
              "vector constant element type mismatch: expected " + type.getElementType()
                  + ", got " + element.getType());
        }
      }
      this.elements = List.copyOf(elements);
    }

    private static String vectorName(List<Constant> elements) {
      return elements.stream()
          .map(element -> element.getType() + ":" + element.getName())
          .collect(java.util.stream.Collectors.joining(",", "<", ">"));
    }
  }
}
