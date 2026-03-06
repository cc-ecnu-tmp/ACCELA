package accela.ir;

/** LLVM IR type system. */
public class Type {
  public enum DataType {
    I1,
    INT,
    I64,
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

  public static final Type I1 = new Type(DataType.I1, null, 0);
  public static final Type INT = new Type(DataType.INT, null, 0);
  public static final Type I64 = new Type(DataType.I64, null, 0);
  public static final Type FLOAT = new Type(DataType.FLOAT, null, 0);
  public static final Type VOID = new Type(DataType.VOID, null, 0);
  public static final Type PTR = new Type(DataType.POINTER, null, 0);

  public static Type pointer(Type inner) {
    return new Type(DataType.POINTER, inner, 0);
  }

  public static Type array(Type inner, int size) {
    return new Type(DataType.ARRAY, inner, size);
  }

  /** Build the LLVM array type for a multi-dim SysY array, e.g. int[3][4] -> [3 x [4 x i32]]. */
  public static Type fromSysY(accela.ast.Ty ty) {
    if (ty.kind == accela.ast.Ty.Kind.ARRAY) {
      Type elem = ty.elem.isFloat() ? FLOAT : INT;
      Type result = elem;
      for (int i = ty.dims.length - 1; i >= 0; i--) result = array(result, ty.dims[i]);
      return result;
    }
    if (ty.isFloat()) return FLOAT;
    if (ty.kind == accela.ast.Ty.Kind.VOID) return VOID;
    return INT;
  }

  /** The scalar element type of this type. */
  public Type scalarType() {
    if (dataType == DataType.ARRAY) return innerType.scalarType();
    return this;
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
      case I1:
        return "i1";
      case INT:
        return "i32";
      case I64:
        return "i64";
      case FLOAT:
        return "float";
      case VOID:
        return "void";
      case POINTER:
        return "ptr";
      case ARRAY:
        return "[" + size + " x " + innerType + "]";
      default:
        return "unknown";
    }
  }
}
