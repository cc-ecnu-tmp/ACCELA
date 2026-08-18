package accela.ast;

import java.util.ArrayList;
import java.util.List;

/**
 * The single AST node type used across parsing, semantic analysis, debugging, and IR lowering.
 *
 * <p>This is a compact representation: instead of one Java class per syntax form, the
 * tree is distinguished by {@link Tag}, and node-specific payload lives in a small shared set of
 * fields such as {@link #s}, {@link #literal}, {@link #ty}, {@link #op}, and {@link #kids}.
 *
 * <p>Because this object flows through multiple stages, some fields are parser-owned while others
 * are filled in later by semantic analysis. The comments below document those cross-stage
 * conventions.
 */
public class Node {
  /** Broad AST category for the node. */
  public enum Tag {
    UNIT,
    FUNC,
    PARM,
    BLOCK,
    DECL_STMT,
    VAR,
    RET,
    BIN,
    CALL,
    REF,
    LIT,
    SUB,
    INIT_LIST,
    IF,
    WHILE,
    BREAK,
    CONT,
    UNARY,
    CAST
  }

  /** Operator kind for expression nodes that carry an operator token. */
  public enum Op {
    ADD,
    SUB,
    MUL,
    DIV,
    MOD,
    LT,
    LE,
    GT,
    GE,
    EQ,
    NE,
    AND,
    OR,
    NOT,
    NEG,
    POS,
    ASSIGN;

    public boolean isRelational() {
      return this == LT || this == LE || this == GT || this == GE || this == EQ || this == NE;
    }

    public boolean isLogical() {
      return this == AND || this == OR;
    }

    public String text() {
      switch (this) {
        case ADD:
          return "+";
        case SUB:
          return "-";
        case MUL:
          return "*";
        case DIV:
          return "/";
        case MOD:
          return "%";
        case LT:
          return "<";
        case LE:
          return "<=";
        case GT:
          return ">";
        case GE:
          return ">=";
        case EQ:
          return "==";
        case NE:
          return "!=";
        case AND:
          return "&&";
        case OR:
          return "||";
        case NOT:
          return "!";
        case NEG:
          return "-";
        case POS:
          return "+";
        case ASSIGN:
          return "=";
      }
      return "?";
    }
  }

  /** Structural kind of this AST node. */
  public final Tag tag;
  /**
   * Small string payload whose meaning depends on {@link #tag}.
   *
   * <p>This is an identifier name for `FUNC` / `PARM` / `VAR` / `REF`. Numeric source spellings
   * are converted to {@link #literal} at the parser boundary.
   */
  public String s;
  /** Typed numeric payload for {@link Tag#LIT}; absent for every other node kind. */
  public LiteralValue literal;
  /**
   * The semantic type of the node when known.
   *
   * <p>This may be absent during early parsing and then filled in by semantic analysis.
   */
  public Ty ty;
  /** Operator payload for `BIN` / `UNARY` style nodes. */
  public Op op;
  /**
   * Boolean side-channel whose exact meaning depends on {@link #tag}.
   *
   * <p>Current uses:
   *
   * <p>- `VAR`: marks a `const` declaration
   *
   * <p>- `PARM`: marks that the first array dimension was omitted in the source parameter list
   */
  public boolean flag;
  /** Marks Tensor declarations/functions until the Tensor lowering pass removes the extension. */
  public boolean tensor;
  /** Distinguishes source {@code @} from ordinary multiplication before Tensor lowering. */
  public boolean tensorMatmul;
  /** Tensor shape before the Tensor desugaring pass replaces it with array/vector storage. */
  public TensorShape tensorShape;
  /**
   * Link to the declaration associated with a reference-like node.
   *
   * <p>This is normally established during semantic analysis so later passes do not need to
   * re-resolve identifiers.
   */
  public Node decl;
  /**
   * Ordered child nodes.
   *
   * <p>The exact layout is tag-specific. For example:
   *
   * <p>- `FUNC`: parameters followed by the body block
   *
   * <p>- `BIN`: left child then right child
   *
   * <p>- `CALL`: callee reference first, then argument expressions
   *
   * <p>- `SUB`: base expression followed by one or more index expressions
   */
  public final List<Node> kids = new ArrayList<>();
  /**
   * Array bound expressions kept on declaration nodes for semantic use.
   *
   * <p>This is separate from {@link #kids} because dimensions are part of declarator shape rather
   * than runtime expression children.
   */
  public List<Node> dimExprs;

  public Node(Tag tag) {
    this.tag = tag;
  }

  public Node(Tag tag, String s) {
    this.tag = tag;
    this.s = s;
  }

  public Node(Tag tag, String s, Ty ty) {
    this.tag = tag;
    this.s = s;
    this.ty = ty;
  }

  public static Node literalFromToken(String spelling) {
    return literal(LiteralValue.parse(spelling));
  }

  public static Node intLiteral(int value) {
    return literal(LiteralValue.ofInt(value));
  }

  public static Node floatLiteral(float value) {
    return literal(LiteralValue.ofFloat(value));
  }

  private static Node literal(LiteralValue value) {
    Node node = new Node(Tag.LIT);
    node.literal = value;
    node.ty = value.isFloat() ? Ty.FLOAT : Ty.INT;
    return node;
  }

  /**
   * Returns the best available type information for this node.
   *
   * <p>Most nodes rely on {@link #ty} after semantic analysis. A few simple nodes infer a useful
   * fallback directly from their payload so debug helpers and early users can still query a type.
   */
  public Ty type() {
    if (ty != null) return ty;
    if (tag == Tag.LIT && literal != null) return literal.isFloat() ? Ty.FLOAT : Ty.INT;
    if (tag == Tag.UNARY && !kids.isEmpty()) return kids.get(0).type();
    return null;
  }
}
