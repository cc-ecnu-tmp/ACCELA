package accela.parse;

import static accela.parse.Lexer.T.*;

import accela.ast.Node;
import accela.ast.Node.Op;
import accela.ast.Node.Tag;
import accela.ast.Ty;
import java.util.ArrayList;
import java.util.List;
import accela.parse.Lexer.T;
import accela.parse.Lexer.Token;

// Parser, needs refactoring
public class Parser {
  private final List<Token> tokens;
  private int pos = 0;

  public Parser(List<Token> tokens) {
    this.tokens = tokens;
  }

  public Node parse() {
    Node unit = new Node(Tag.UNIT);
    while (peek().type != EOF) {
      if (isFuncDef()) unit.kids.add(parseFuncDef());
      else unit.kids.addAll(parseVarDeclList());
    }
    return unit;
  }

  private boolean isFuncDef() {
    int la = 0;
    if (peek(la).type == CONST) return false;
    if (peek(la).type == INT || peek(la).type == VOID || peek(la).type == FLOAT) {
      la++;
      if (peek(la).type == IDENT) return peek(la + 1).type == LP;
    }
    return false;
  }

  private Node parseFuncDef() {
    Token tt = peek();
    if (tt.type == INT) consume(INT);
    else if (tt.type == FLOAT) consume(FLOAT);
    else consume(VOID);
    Ty retTy = tt.type == INT ? Ty.INT : tt.type == FLOAT ? Ty.FLOAT : Ty.VOID;
    Node func = new Node(Tag.FUNC, consume(IDENT).text, retTy);
    consume(LP);
    if (peek().type != RP) {
      func.kids.add(parseFuncFParam());
      while (peek().type == COMMA) {
        consume(COMMA);
        func.kids.add(parseFuncFParam());
      }
    }
    consume(RP);
    func.kids.add(parseBlock());
    return func;
  }

  private Node parseFuncFParam() {
    Token tt = peek();
    if (tt.type == INT) consume(INT);
    else consume(FLOAT);
    Node p = new Node(Tag.PARM, consume(IDENT).text, tt.type == FLOAT ? Ty.FLOAT : Ty.INT);
    List<Node> dimExprs = new ArrayList<>();
    if (peek().type == LS) {
      consume(LS);
      if (peek().type != RS) dimExprs.add(parseExp());
      else p.flag = true; // please see `firstDimEmpty`
      consume(RS);
      while (peek().type == LS) {
        consume(LS);
        dimExprs.add(parseExp());
        consume(RS);
      }
    }
    p.dimExprs = dimExprs;
    return p;
  }

  private Node parseBlock() {
    consume(LB);
    Node block = new Node(Tag.BLOCK);
    while (peek().type != RB) {
      Node item = parseBlockItem();
      if (item != null) block.kids.add(item);
    }
    consume(RB);
    return block;
  }

  private Node parseBlockItem() {
    Token tok = peek();
    if (tok.type == INT || tok.type == CONST || tok.type == FLOAT) {
      Node ds = new Node(Tag.DECL_STMT);
      ds.kids.addAll(parseVarDeclList());
      return ds;
    }
    return parseStmt();
  }

  private List<Node> parseVarDeclList() {
    boolean isConst = false;
    if (peek().type == CONST) {
      consume(CONST);
      isConst = true;
    }
    Token tt = peek();
    if (tt.type == INT) consume(INT);
    else if (tt.type == FLOAT) consume(FLOAT);
    else consume(VOID);
    Ty baseTy = tt.type == FLOAT ? Ty.FLOAT : tt.type == VOID ? Ty.VOID : Ty.INT;
    List<Node> decls = new ArrayList<>();
    while (true) {
      decls.add(parseVarDef(baseTy, isConst));
      if (peek().type == COMMA) consume(COMMA);
      else break;
    }
    consume(SEMI);
    return decls;
  }

  private Node parseVarDef(Ty baseTy, boolean isConst) {
    Node v = new Node(Tag.VAR, consume(IDENT).text, baseTy);
    v.flag = isConst;
    List<Node> dimExprs = new ArrayList<>();
    while (peek().type == LS) {
      consume(LS);
      dimExprs.add(parseExp());
      consume(RS);
    }
    v.dimExprs = dimExprs;
    if (peek().type == EQ) {
      consume(EQ);
      v.kids.add(parseInitVal());
    }
    return v;
  }

  private Node parseInitVal() {
    if (peek().type == LB) return parseRawInitList();
    return parseExp();
  }

  private Node parseRawInitList() {
    consume(LB);
    Node il = new Node(Tag.INIT_LIST);
    if (peek().type != RB) {
      il.kids.add(parseInitVal());
      while (peek().type == COMMA) {
        consume(COMMA);
        il.kids.add(parseInitVal());
      }
    }
    consume(RB);
    return il;
  }

  private Node parseStmt() {
    switch (peek().type) {
      case IF:
        return parseIfStmt();
      case WHILE:
        return parseWhileStmt();
      case BREAK:
        consume(BREAK);
        consume(SEMI);
        return new Node(Tag.BREAK);
      case CONTINUE:
        consume(CONTINUE);
        consume(SEMI);
        return new Node(Tag.CONT);
      case RETURN:
        {
          consume(RETURN);
          Node ret = new Node(Tag.RET);
          if (peek().type != SEMI) ret.kids.add(parseExp());
          consume(SEMI);
          return ret;
        }
      case LB:
        return parseBlock();
      case SEMI:
        consume(SEMI);
        return null;
      default:
        Node exp = parseExp();
        consume(SEMI);
        return exp;
    }
  }

  private Node parseIfStmt() {
    consume(IF);
    consume(LP);
    Node n = new Node(Tag.IF);
    n.kids.add(parseExp());
    consume(RP);
    Node then = parseStmt();
    n.kids.add(then != null ? then : new Node(Tag.BLOCK));
    if (peek().type == ELSE) {
      consume(ELSE);
      Node els = parseStmt();
      n.kids.add(els != null ? els : new Node(Tag.BLOCK));
    }
    return n;
  }

  private Node parseWhileStmt() {
    consume(WHILE);
    consume(LP);
    Node n = new Node(Tag.WHILE);
    n.kids.add(parseExp());
    consume(RP);
    Node body = parseStmt();
    n.kids.add(body != null ? body : new Node(Tag.BLOCK));
    return n;
  }

  // Pratt: precedence table built from T.ordinal()
  static final int[] PREC = new int[36];
  static final Op[] BIN_OP = new Op[36];

  static {
    PREC[OR.ordinal()] = 1;
    PREC[AND.ordinal()] = 2;
    PREC[EQEQ.ordinal()] = 3;
    PREC[NE.ordinal()] = 3;
    PREC[LT.ordinal()] = 4;
    PREC[GT.ordinal()] = 4;
    PREC[LE.ordinal()] = 4;
    PREC[GE.ordinal()] = 4;
    PREC[PLUS.ordinal()] = 5;
    PREC[MINUS.ordinal()] = 5;
    PREC[STAR.ordinal()] = 6;
    PREC[SLASH.ordinal()] = 6;
    PREC[PERCENT.ordinal()] = 6;
    BIN_OP[PLUS.ordinal()] = Op.ADD;
    BIN_OP[MINUS.ordinal()] = Op.SUB;
    BIN_OP[STAR.ordinal()] = Op.MUL;
    BIN_OP[SLASH.ordinal()] = Op.DIV;
    BIN_OP[PERCENT.ordinal()] = Op.MOD;
    BIN_OP[LT.ordinal()] = Op.LT;
    BIN_OP[LE.ordinal()] = Op.LE;
    BIN_OP[GT.ordinal()] = Op.GT;
    BIN_OP[GE.ordinal()] = Op.GE;
    BIN_OP[EQEQ.ordinal()] = Op.EQ;
    BIN_OP[NE.ordinal()] = Op.NE;
    BIN_OP[AND.ordinal()] = Op.AND;
    BIN_OP[OR.ordinal()] = Op.OR;
  }

  private Node parseExp() {
    Node lhs = parseExpr(1);
    if (peek().type == EQ) {
      consume(EQ);
      Node assign = new Node(Tag.BIN);
      assign.op = Op.ASSIGN;
      assign.kids.add(lhs);
      assign.kids.add(parseExp());
      return assign;
    }
    return lhs;
  }

  private Node parseExpr(int minPrec) {
    Node lhs = parseUnaryExp();
    while (PREC[peek().type.ordinal()] >= minPrec) {
      Token op = consume(peek().type);
      Node bin = new Node(Tag.BIN);
      bin.op = BIN_OP[op.type.ordinal()];
      bin.kids.add(lhs);
      bin.kids.add(parseExpr(PREC[op.type.ordinal()] + 1));
      lhs = bin;
    }
    return lhs;
  }

  private Node parseUnaryExp() {
    Token tok = peek();
    if (tok.type == PLUS || tok.type == MINUS || tok.type == NOT) {
      consume(tok.type);
      Node u = new Node(Tag.UNARY);
      u.op = tok.type == PLUS ? Op.POS : tok.type == MINUS ? Op.NEG : Op.NOT;
      u.kids.add(parseUnaryExp());
      return u;
    }
    if (tok.type == IDENT && peek(1).type == LP) return parseFuncCall();
    Node primary = parsePrimaryExp();
    while (peek().type == LS) {
      consume(LS);
      Node sub = new Node(Tag.SUB);
      sub.kids.add(primary);
      sub.kids.add(parseExp());
      consume(RS);
      primary = sub;
    }
    return primary;
  }

  private Node parseFuncCall() {
    Node call = new Node(Tag.CALL);
    call.kids.add(new Node(Tag.REF, consume(IDENT).text));
    consume(LP);
    if (peek().type != RP) {
      call.kids.add(parseExp());
      while (peek().type == COMMA) {
        consume(COMMA);
        call.kids.add(parseExp());
      }
    }
    consume(RP);
    return call;
  }

  private Node parsePrimaryExp() {
    Token tok = peek();
    if (tok.type == LP) {
      consume(LP);
      Node e = parseExp();
      consume(RP);
      return e;
    }
    if (tok.type == NUM) {
      consume(NUM);
      return new Node(Tag.LIT, tok.text);
    }
    if (tok.type == IDENT) {
      consume(IDENT);
      return new Node(Tag.REF, tok.text);
    }
    throw new RuntimeException("Unexpected token: " + tok);
  }

  private Token consume(T type) {
    Token tok = peek();
    if (tok.type != type) throw new RuntimeException("Expected " + type + " but got " + tok.type);
    pos++;
    return tok;
  }

  private Token peek() {
    return peek(0);
  }

  private Token peek(int n) {
    return pos + n >= tokens.size() ? tokens.get(tokens.size() - 1) : tokens.get(pos + n);
  }
}
