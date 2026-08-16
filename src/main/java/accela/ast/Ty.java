package accela.ast;

/**
 * Frontend type model used by the parser, semantic analysis, AST dumper, and interpreter.
 *
 * <p>This is intentionally separate from the IR/backend type system. It represents source-level
 * SysY types and keeps enough array shape information for semantic checks and AST-to-IR lowering.
 */
public final class Ty {
  /** Source-language type category. */
  public enum Kind {
    INT,
    FLOAT,
    VOID,
    ARRAY,
    VECTOR
  }

  /** Top-level kind of this type. */
  public final Kind kind;
  /** Element type for arrays; {@code null} for scalar and void types. */
  public final Ty elem;
  /** Array extents in source order; {@code null} for non-array types. */
  public final int[] dims;
  /** Fixed lane count for vectors; zero for non-vector types. */
  public final int lanes;

  public static final Ty INT = new Ty(Kind.INT);
  public static final Ty FLOAT = new Ty(Kind.FLOAT);
  public static final Ty VOID = new Ty(Kind.VOID);

  private Ty(Kind kind) {
    this.kind = kind;
    this.elem = null;
    this.dims = null;
    this.lanes = 0;
  }

  private Ty(Kind kind, Ty elem, int[] dims, int lanes) {
    this.kind = kind;
    this.elem = elem;
    this.dims = dims;
    this.lanes = lanes;
  }

  /** Builds an array type whose elements are {@code elem} and whose extents are {@code dims}. */
  public static Ty array(Ty elem, int... dims) {
    return new Ty(Kind.ARRAY, elem, dims, 0);
  }

  /** Builds a fixed-width homogeneous numeric vector type. */
  public static Ty vector(Ty elem, int lanes) {
    if (elem != INT && elem != FLOAT)
      throw new IllegalArgumentException("vector element must be int or float");
    if (lanes <= 0) throw new IllegalArgumentException("vector lane count must be positive");
    return new Ty(Kind.VECTOR, elem, null, lanes);
  }

  /** Builds a contextual vector type whose lane count will be inferred by semantic analysis. */
  public static Ty inferredVector(Ty elem) {
    if (elem != INT && elem != FLOAT)
      throw new IllegalArgumentException("vector element must be int or float");
    return new Ty(Kind.VECTOR, elem, null, 0);
  }

  /**
   * Removes one layer of array indexing.
   *
   * <p>Examples:
   *
   * <p>- `int[3][4] -> int[4]`
   *
   * <p>- `int[4] -> int`
   */
  public Ty deref() {
    if (kind == Kind.VECTOR) return elem;
    if (kind != Kind.ARRAY) return this;
    if (dims.length == 1) return elem;
    int[] rest = new int[dims.length - 1];
    System.arraycopy(dims, 1, rest, 0, rest.length);
    return array(elem, rest);
  }

  /** Returns the total number of scalar elements contained in this array type. */
  public int flatSize() {
    if (kind == Kind.VECTOR) return lanes;
    if (dims == null) return 1;
    int s = elem.flatSize();
    for (int d : dims) s *= d;
    return s;
  }

  /** Returns the underlying scalar kind, peeling away any array nesting. */
  public Kind scalar() {
    return kind == Kind.ARRAY || kind == Kind.VECTOR ? elem.scalar() : kind;
  }

  public boolean isFloat() {
    return scalar() == Kind.FLOAT;
  }

  public boolean isInt() {
    return scalar() == Kind.INT;
  }

  public boolean isArray() {
    return kind == Kind.ARRAY;
  }

  public boolean isVector() {
    return kind == Kind.VECTOR;
  }

  @Override
  public String toString() {
    if (kind == Kind.ARRAY) {
      StringBuilder sb = new StringBuilder(elem.toString());
      for (int d : dims) sb.append('[').append(d).append(']');
      return sb.toString();
    }
    if (kind == Kind.VECTOR) return lanes == 0 ? "vector " + elem : elem + Integer.toString(lanes);
    return kind.name().toLowerCase();
  }

  @Override
  public boolean equals(Object o) {
    if (this == o) return true;
    if (!(o instanceof Ty)) return false;
    Ty t = (Ty) o;
    if (kind != t.kind) return false;
    if (kind == Kind.ARRAY) return elem.equals(t.elem) && java.util.Arrays.equals(dims, t.dims);
    if (kind == Kind.VECTOR) return elem.equals(t.elem) && lanes == t.lanes;
    return true;
  }

  @Override
  public int hashCode() {
    int h = kind.ordinal();
    if (kind == Kind.ARRAY) h = h * 31 + elem.hashCode() + java.util.Arrays.hashCode(dims);
    if (kind == Kind.VECTOR) h = h * 31 + elem.hashCode() * 31 + lanes;
    return h;
  }
}
