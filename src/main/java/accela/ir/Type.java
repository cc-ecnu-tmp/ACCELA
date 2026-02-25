package accela.ir;

// TODO: This is a work in progress, design can change
// We *really* need to refactor this one.
public class Type {
  public enum DataType {
    INT,
    FLOAT,
    VOID,
    POINTER,
    ARRAY
  }

  public final DataType dataType;
  public final Type innerType;
  public final int size;

  private Type(DataType dataType, Type innerType, int size) {
    this.dataType = dataType;
    this.innerType = innerType;
    this.size = size;
  }

  public static final Type INT = new Type(DataType.INT, null, 0);
  public static final Type FLOAT = new Type(DataType.FLOAT, null, 0);
  public static final Type VOID = new Type(DataType.VOID, null, 0);

  public static Type from(String s) {
    if (s == null) return VOID;
    if (s.equals("int")) return INT;
    if (s.equals("float")) return FLOAT;
    if (s.equals("void")) return VOID;
    if (s.endsWith("*")) return pointer(from(s.substring(0, s.length() - 1)));
    if (s.contains("[") && s.endsWith("]")) {
      int open = s.indexOf("[");
      int close = s.lastIndexOf("]");
      String inner = s.substring(0, open);
      int size = Integer.parseInt(s.substring(open + 1, close));
      return array(from(inner), size);
    }
    return VOID;
  }

  public static Type pointer(Type inner) {
    return new Type(DataType.POINTER, inner, 0);
  }

  public static Type array(Type inner, int size) {
    return new Type(DataType.ARRAY, inner, size);
  }

  public boolean isInt() {
    return dataType == DataType.INT;
  }

  public boolean isFloat() {
    return dataType == DataType.FLOAT;
  }

  public boolean isPointer() {
    return dataType == DataType.POINTER;
  }

  public boolean isArray() {
    return dataType == DataType.ARRAY;
  }

  @Override
  public String toString() {
    switch (dataType) {
      case INT:
        return "i32";
      case FLOAT:
        return "float";
      case VOID:
        return "void";
      case POINTER:
        return innerType + "*";
      case ARRAY:
        return "[" + size + " x " + innerType + "]";
      default:
        return "unknown";
    }
  }
}
