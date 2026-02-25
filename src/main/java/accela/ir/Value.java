package accela.ir;

public class Value {
  public final Type type;
  private final int iVal;
  private final float fVal;
  private final Object ptrVal; // mainly for interpreter

  private Value(Type type, int iVal, float fVal, Object ptrVal) {
    this.type = type;
    this.iVal = iVal;
    this.fVal = fVal;
    this.ptrVal = ptrVal;
  }

  public static Value of(int v) {
    return new Value(Type.INT, v, 0, null);
  }

  public static Value of(float v) {
    return new Value(Type.FLOAT, 0, v, null);
  }

  public static Value of(boolean v) {
    return new Value(Type.INT, v ? 1 : 0, 0, null);
  }

  public static Value ptr(Object addr) {
    return new Value(Type.pointer(Type.INT), 0, 0, addr);
  }

  public int asInt() {
    return type.isFloat() ? (int) fVal : iVal;
  }

  public float asFloat() {
    return type.isInt() ? (float) iVal : fVal;
  }

  public boolean asBool() {
    return type.isInt() ? iVal != 0 : fVal != 0.0f;
  }

  public Object asPtr() {
    return ptrVal;
  }

  public Value add(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() + r.asFloat());
    return Value.of(this.asInt() + r.asInt());
  }

  public Value sub(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() - r.asFloat());
    return Value.of(this.asInt() - r.asInt());
  }

  public Value mul(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() * r.asFloat());
    return Value.of(this.asInt() * r.asInt());
  }

  public Value div(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() / r.asFloat());
    return Value.of(this.asInt() / r.asInt());
  }

  public Value mod(Value r) {
    return Value.of(this.asInt() % r.asInt()); // runtime safety?
  }

  public Value eq(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() == r.asFloat());
    return Value.of(this.asInt() == r.asInt());
  }

  public Value ne(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() != r.asFloat());
    return Value.of(this.asInt() != r.asInt());
  }

  public Value lt(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() < r.asFloat());
    return Value.of(this.asInt() < r.asInt());
  }

  public Value le(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() <= r.asFloat());
    return Value.of(this.asInt() <= r.asInt());
  }

  public Value gt(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() > r.asFloat());
    return Value.of(this.asInt() > r.asInt());
  }

  public Value ge(Value r) {
    if (this.type.isFloat() || r.type.isFloat()) return Value.of(this.asFloat() >= r.asFloat());
    return Value.of(this.asInt() >= r.asInt());
  }

  @Override
  public String toString() {
    if (type.isInt()) return String.valueOf(iVal);
    if (type.isFloat()) return String.valueOf(fVal);
    return ptrVal.toString();
  }
}
