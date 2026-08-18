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

/**
 * Hand-written parser that builds the project's AST directly.
 *
 * <p>The parser uses ordinary recursive descent for declarations/statements and a Pratt-style
 * precedence parser for expressions. The resulting AST is intentionally close to the surface syntax;
 * later normalization such as type insertion, declaration binding, and initializer reshaping is
 * handled by {@link Sema}.
 */
public class Parser {
  private final List<Token> tokens;
  private int pos = 0;
  private boolean parsedTensorType;

  public Parser(List<Token> tokens) {
    this.tokens = tokens;
  }

  /** Parses a whole translation unit into a {@code UNIT} node. */
  public Node parse() {
    Node unit = new Node(Tag.UNIT);
    while (peek().type != EOF) {
      if (isFuncDef()) unit.kids.add(parseFuncDef());
      else unit.kids.addAll(parseVarDeclList());
    }
    return unit;
  }

  /** Distinguishes a function definition from a variable declaration using short lookahead. */
  private boolean isFuncDef() {
    int la = 0;
    if (peek(la).type == CONST) return false;
    if (peek(la).type == IDENT && peek(la).text.equals("tensor")) {
      la++;
      if (peek(la).type != INT && peek(la).type != FLOAT) return false;
      la++;
      return peek(la).type == IDENT && peek(la + 1).type == LP;
    }
    if (peek(la).type == INT || peek(la).type == VOID || peek(la).type == FLOAT) {
      la++;
      if (peek(la).type == IDENT) return peek(la + 1).type == LP;
    }
    return false;
  }

  /** Parses a function definition, storing parameters as leading children and the body last. */
  private Node parseFuncDef() {
    Ty retTy = parseVarType();
    boolean tensor = parsedTensorType;
    Node func = new Node(Tag.FUNC, consume(IDENT).text, retTy);
    func.tensor = tensor;
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

  /**
   * Parses one formal parameter.
   *
   * <p>Array-parameter shape is intentionally recorded in a raw frontend form here. Sema later
   * resolves dimension expressions and turns it into the final parameter type.
   */
  private Node parseFuncFParam() {
    Ty paramType = parseVarType();
    boolean tensor = parsedTensorType;
    if (paramType == Ty.VOID) throw new RuntimeException("Function parameter cannot be void");
    Node p = new Node(Tag.PARM, consume(IDENT).text, paramType);
    p.tensor = tensor;
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

  /** Parses a declaration statement or ordinary statement appearing inside a block. */
  private Node parseBlockItem() {
    Token tok = peek();
    if (tok.type == CONST || isTypeStart(0)) {
      Node ds = new Node(Tag.DECL_STMT);
      ds.kids.addAll(parseVarDeclList());
      return ds;
    }
    return parseStmt();
  }

  /** Parses a comma-separated declaration list sharing the same base type and constness. */
  private List<Node> parseVarDeclList() {
    boolean isConst = false;
    if (peek().type == CONST) {
      consume(CONST);
      isConst = true;
    }
    Ty baseTy = parseVarType();
    boolean tensor = parsedTensorType;
    List<Node> decls = new ArrayList<>();
    while (true) {
      decls.add(parseVarDef(baseTy, isConst, tensor));
      if (peek().type == COMMA) consume(COMMA);
      else break;
    }
    consume(SEMI);
    return decls;
  }

  /** Parses scalar types plus the two provisional fixed-vector spellings. */
  private Ty parseVarType() {
    parsedTensorType = false;
    Token tok = peek();
    if (tok.type == IDENT && tok.text.equals("tensor")) {
      consume(IDENT);
      parsedTensorType = true;
      tok = peek();
      if (tok.type != INT && tok.type != FLOAT)
        throw new RuntimeException("Expected int or float after tensor");
    }
    if (tok.type == INT) {
      consume(INT);
      return Ty.INT;
    }
    if (tok.type == FLOAT) {
      consume(FLOAT);
      return Ty.FLOAT;
    }
    if (tok.type == VOID) {
      consume(VOID);
      return Ty.VOID;
    }
    if (tok.type == IDENT && tok.text.equals("vector")) {
      consume(IDENT);
      Token elem = peek();
      if (elem.type != INT && elem.type != FLOAT)
        throw new RuntimeException("Expected int or float after vector");
      consume(elem.type);
      return Ty.inferredVector(elem.type == FLOAT ? Ty.FLOAT : Ty.INT);
    }
    int lanes = vectorLanes(tok.text);
    if (tok.type == IDENT && lanes > 0) {
      consume(IDENT);
      return Ty.vector(tok.text.startsWith("float") ? Ty.FLOAT : Ty.INT, lanes);
    }
    throw new RuntimeException("Expected type but got " + tok);
  }

  private boolean isTypeStart(int lookahead) {
    Token tok = peek(lookahead);
    if (tok.type == INT || tok.type == FLOAT || tok.type == VOID) return true;
    if (tok.type != IDENT) return false;
    if (tok.text.equals("tensor")) {
      T next = peek(lookahead + 1).type;
      return next == INT || next == FLOAT;
    }
    if (tok.text.equals("vector")) {
      T next = peek(lookahead + 1).type;
      return next == INT || next == FLOAT;
    }
    return vectorLanes(tok.text) > 0;
  }

  private static int vectorLanes(String spelling) {
    String suffix;
    if (spelling.startsWith("int")) suffix = spelling.substring(3);
    else if (spelling.startsWith("float")) suffix = spelling.substring(5);
    else return 0;
    if (suffix.isEmpty()) return 0;
    for (int i = 0; i < suffix.length(); i++) if (!Character.isDigit(suffix.charAt(i))) return 0;
    try {
      int lanes = Integer.parseInt(suffix);
      return lanes > 0 ? lanes : 0;
    } catch (NumberFormatException ignored) {
      return 0;
    }
  }

  /** Parses one declared variable/constant, including optional dimensions and initializer. */
  private Node parseVarDef(Ty baseTy, boolean isConst, boolean tensor) {
    Node v = new Node(Tag.VAR, consume(IDENT).text, baseTy);
    v.flag = isConst;
    v.tensor = tensor;
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

  /** Parses either an expression initializer or a raw brace initializer list. */
  private Node parseInitVal() {
    if (peek().type == LB) return parseRawInitList();
    return parseExp();
  }

  /** Parses a brace initializer without doing semantic reshaping yet. */
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

  /** Parses one statement form. Empty statements are represented as {@code null}. */
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

  /** Parses an if statement and normalizes missing branches to empty blocks. */
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

  /** Parses a while statement. */
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

  /**
   * Pratt precedence table keyed by token-kind ordinal.
   *
   * <p>This is why {@link Lexer.T} ordering is effectively part of the parser contract.
   */
  static final int[] PREC = new int[T.values().length];
  static final Op[] BIN_OP = new Op[T.values().length];

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
    PREC[AT.ordinal()] = 6;
    BIN_OP[PLUS.ordinal()] = Op.ADD;
    BIN_OP[MINUS.ordinal()] = Op.SUB;
    BIN_OP[STAR.ordinal()] = Op.MUL;
    BIN_OP[SLASH.ordinal()] = Op.DIV;
    BIN_OP[PERCENT.ordinal()] = Op.MOD;
    BIN_OP[AT.ordinal()] = Op.MUL;
    BIN_OP[LT.ordinal()] = Op.LT;
    BIN_OP[LE.ordinal()] = Op.LE;
    BIN_OP[GT.ordinal()] = Op.GT;
    BIN_OP[GE.ordinal()] = Op.GE;
    BIN_OP[EQEQ.ordinal()] = Op.EQ;
    BIN_OP[NE.ordinal()] = Op.NE;
    BIN_OP[AND.ordinal()] = Op.AND;
    BIN_OP[OR.ordinal()] = Op.OR;
  }

  /**
   * Parses an expression, treating assignment as the lowest-precedence right-associative operator.
   *
   * <p>All other binary operators are handled by {@link #parseExpr(int)}.
   */
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

  /**
   * Pratt expression parser.
   *
   * <p>{@code minPrec} is the usual binding-power threshold: while the next token has at least that
   * precedence, parse it as a binary operator and recurse on the RHS with tighter binding.
   */
  private Node parseExpr(int minPrec) {
    Node lhs = parseUnaryExp();
    while (PREC[peek().type.ordinal()] >= minPrec) {
      Token op = consume(peek().type);
      Node bin = new Node(Tag.BIN);
      bin.op = BIN_OP[op.type.ordinal()];
      bin.tensorMatmul = op.type == AT;
      bin.kids.add(lhs);
      bin.kids.add(parseExpr(PREC[op.type.ordinal()] + 1));
      lhs = bin;
    }
    return lhs;
  }

  /**
   * Parses unary expressions and postfix subscripting.
   *
   * <p>Repeated subscripting is left-associated here as nested {@code SUB} nodes. Sema later
   * flattens chains such as {@code a[i][j]} into one node carrying all indices.
   */
  private Node parseUnaryExp() {
    Token tok = peek();
    if (tok.type == PLUS || tok.type == MINUS || tok.type == NOT) {
      consume(tok.type);
      Node u = new Node(Tag.UNARY);
      u.op = tok.type == PLUS ? Op.POS : tok.type == MINUS ? Op.NEG : Op.NOT;
      u.kids.add(parseUnaryExp());
      return u;
    }
    Node primary = tok.type == IDENT && peek(1).type == LP ? parseFuncCall() : parsePrimaryExp();
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

  /** Parses a direct function call. */
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

  /** Parses parenthesized expressions, references, and literals. */
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
      return Node.literalFromToken(tok.text);
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
