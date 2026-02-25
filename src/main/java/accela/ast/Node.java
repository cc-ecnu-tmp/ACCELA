package accela.ast;

import java.util.ArrayList;
import java.util.List;

// TODO: add Design markdown file
public class Node {
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

  public final Tag tag;
  public String s;
  public Ty ty;
  public Op op;
  public boolean flag;
  public Node decl;
  public final List<Node> kids = new ArrayList<>();
  public List<Node> dimExprs; // S~e~m~a~

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

  public Ty type() {
    if (ty != null) return ty;
    if (tag == Tag.LIT) return isFloatLit(s) ? Ty.FLOAT : Ty.INT;
    if (tag == Tag.UNARY && !kids.isEmpty()) return kids.get(0).type();
    return null;
  }

  static boolean isFloatLit(String v) {
    return v.contains(".")
        || v.contains("e")
        || v.contains("E")
        || v.contains("p")
        || v.contains("P");
  }
}
