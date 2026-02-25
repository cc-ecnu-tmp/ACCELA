package ast;

import java.io.PrintStream;

// For debugging only
public class AstDumper {
  private final PrintStream out;
  private int indent = 0;

  public AstDumper(PrintStream out) {
    this.out = out;
  }

  public void dump(Node n) {
    if (n == null) return;
    String pfx = "  ".repeat(indent);
    switch (n.tag) {
      case UNIT:
        out.println(pfx + "CompUnit");
        break;
      case FUNC:
        out.println(pfx + "FuncDef: " + n.s + ", type: " + n.ty);
        break;
      case PARM:
        out.println(pfx + "ParmVar: " + n.s + ", type: " + n.ty);
        break;
      case BLOCK:
        out.println(pfx + "Block");
        break;
      case DECL_STMT:
        out.println(pfx + "DeclStmt");
        break;
      case VAR:
        out.println(pfx + "VarDecl: " + n.s + ", type: " + (n.flag ? "const " : "") + n.ty);
        break;
      case RET:
        out.println(pfx + "ReturnStmt");
        break;
      case BIN:
        out.println(pfx + "BinaryOperator: " + n.op.text() + ", type: " + n.ty);
        break;
      case CALL:
        out.println(pfx + "CallExpr: type: " + n.ty);
        break;
      case REF:
        out.println(pfx + "DeclRef: " + n.s + ", type: " + n.ty);
        break;
      case LIT:
        out.println(pfx + "IntegerLiteral: " + n.s);
        break;
      case SUB:
        out.println(pfx + "ArraySubscript: type: " + n.ty);
        break;
      case INIT_LIST:
        out.println(pfx + "InitList: type: " + n.ty);
        break;
      case IF:
        out.println(pfx + "IfStmt");
        break;
      case WHILE:
        out.println(pfx + "WhileStmt");
        break;
      case BREAK:
        out.println(pfx + "BreakStmt");
        break;
      case CONT:
        out.println(pfx + "ContinueStmt");
        break;
      case UNARY:
        out.println(pfx + "UnaryOperator: " + n.op.text());
        break;
      case CAST:
        out.println(pfx + "ImplicitCast: " + n.ty);
        break;
    }
    indent++;
    for (Node kid : n.kids) dump(kid);
    indent--;
  }
}
