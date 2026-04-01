package accela.ir;

/**
 * The project IR type system.
 *
 * <p>This layer is intentionally separate from the frontend {@code accela.ast.Ty}: the frontend
 * models source-language types, while this class models the smaller set of types that appear in
 * the LLVM-like IR and backend pipeline.
 */
public class Type {
  /** Coarse IR type category. */
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

  /** Canonical i1 type used for boolean-like IR results. */
  public static final Type I1 = new Type(DataType.I1, null, 0);
  /** Canonical i32 type used for SysY integers. */
  public static final Type INT = new Type(DataType.INT, null, 0);
  /** Canonical i64 type used where the backend needs wider integer values. */
  public static final Type I64 = new Type(DataType.I64, null, 0);
  /** Canonical float type used for SysY floating-point values. */
  public static final Type FLOAT = new Type(DataType.FLOAT, null, 0);
  /** Canonical void type for statements and void-returning calls/functions. */
  public static final Type VOID = new Type(DataType.VOID, null, 0);
  /** Opaque pointer type used by this IR. */
  public static final Type PTR = new Type(DataType.POINTER, null, 0);

  /** Builds a pointer type that conceptually points at {@code inner}. */
  public static Type pointer(Type inner) {
    return new Type(DataType.POINTER, inner, 0);
  }

  /** Builds an array type of the form {@code [size x inner]}. */
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

  /** Returns the innermost non-array element type. */
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
