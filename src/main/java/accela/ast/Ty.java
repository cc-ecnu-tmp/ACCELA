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
    ARRAY
  }

  /** Top-level kind of this type. */
  public final Kind kind;
  /** Element type for arrays; {@code null} for scalar and void types. */
  public final Ty elem;
  /** Array extents in source order; {@code null} for non-array types. */
  public final int[] dims;

  public static final Ty INT = new Ty(Kind.INT);
  public static final Ty FLOAT = new Ty(Kind.FLOAT);
  public static final Ty VOID = new Ty(Kind.VOID);

  private Ty(Kind kind) {
    this.kind = kind;
    this.elem = null;
    this.dims = null;
  }

  private Ty(Kind kind, Ty elem, int[] dims) {
    this.kind = kind;
    this.elem = elem;
    this.dims = dims;
  }

  /** Builds an array type whose elements are {@code elem} and whose extents are {@code dims}. */
  public static Ty array(Ty elem, int... dims) {
    return new Ty(Kind.ARRAY, elem, dims);
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
    if (kind != Kind.ARRAY) return this;
    if (dims.length == 1) return elem;
    int[] rest = new int[dims.length - 1];
    System.arraycopy(dims, 1, rest, 0, rest.length);
    return array(elem, rest);
  }

  /** Returns the total number of scalar elements contained in this array type. */
  public int flatSize() {
    if (dims == null) return 1;
    int s = 1;
    for (int d : dims) s *= d;
    return s;
  }

  /** Returns the underlying scalar kind, peeling away any array nesting. */
  public Kind scalar() {
    return kind == Kind.ARRAY ? elem.kind : kind;
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

  @Override
  public String toString() {
    if (kind == Kind.ARRAY) {
      StringBuilder sb = new StringBuilder(elem.toString());
      for (int d : dims) sb.append('[').append(d).append(']');
      return sb.toString();
    }
    return kind.name().toLowerCase();
  }

  /**
   * Parses a frontend type spelling into a {@link Ty}.
   *
   * <p>This helper ignores qualifiers such as {@code const}; those are tracked separately on AST
   * declaration nodes.
   */
  public static Ty fromString(String s) {
    String clean = s.startsWith("const ") ? s.substring(6) : s;
    Kind base =
        clean.startsWith("float") ? Kind.FLOAT : clean.startsWith("void") ? Kind.VOID : Kind.INT;
    if (!clean.contains("[")) return base == Kind.INT ? INT : base == Kind.FLOAT ? FLOAT : VOID;
    java.util.List<Integer> dimList = new java.util.ArrayList<>();
    java.util.regex.Matcher m = java.util.regex.Pattern.compile("\\[(\\d*)\\]").matcher(clean);
    while (m.find()) {
      String d = m.group(1);
      dimList.add(d.isEmpty() ? 0 : Integer.parseInt(d));
    }
    int[] dims = dimList.stream().mapToInt(Integer::intValue).toArray();
    return array(base == Kind.FLOAT ? FLOAT : INT, dims);
  }

  @Override
  public boolean equals(Object o) {
    if (this == o) return true;
    if (!(o instanceof Ty)) return false;
    Ty t = (Ty) o;
    if (kind != t.kind) return false;
    if (kind == Kind.ARRAY) return elem.equals(t.elem) && java.util.Arrays.equals(dims, t.dims);
    return true;
  }

  @Override
  public int hashCode() {
    int h = kind.ordinal();
    if (kind == Kind.ARRAY) h = h * 31 + elem.hashCode() + java.util.Arrays.hashCode(dims);
    return h;
  }
}
