package accela.parse;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Hand-written lexer for the SysY-like frontend.
 *
 * <p>The lexer is intentionally small and direct: it scans the whole source string once and emits a
 * flat token stream consumed by {@link Parser}. Number literals are preserved mostly as text and are
 * interpreted later by Sema/IR lowering, which keeps tokenization simple.
 */
public class Lexer {
  /**
   * Token kinds.
   *
   * <p>The order matters because the parser's precedence table indexes by {@code ordinal()}, so
   * inserting/reordering entries without updating the parser will silently break expression parsing.
   */
  public enum T {
    INT,
    VOID,
    CONST,
    IF,
    ELSE,
    WHILE,
    BREAK,
    CONTINUE,
    RETURN,
    FLOAT,
    IDENT,
    NUM,
    LP,
    RP,
    LB,
    RB,
    LS,
    RS,
    COMMA,
    SEMI,
    PLUS,
    MINUS,
    STAR,
    SLASH,
    PERCENT,
    AT,
    LT,
    LE,
    GT,
    GE,
    EQ,
    EQEQ,
    NOT,
    NE,
    AND,
    OR,
    EOF
  }

  /** Keyword lookup table. */
  static final Map<String, T> KW = new HashMap<>();

  /** Builds the keyword table from the leading keyword region of {@link T}. */
  static {
    for (int i = 0; i <= T.FLOAT.ordinal(); i++) {
      T t = T.values()[i];
      KW.put(t.name().toLowerCase(), t);
    }
  }

  /** Single-character punctuation/operator lookup for ASCII input. */
  static final T[] CT = new T[128];
  /** Two-character operator lookup keyed by the first character. */
  static final T[] CT2 = new T[128];

  /** Precomputes constant-time punctuation/operator classification tables. */
  static {
    String o = "+-*/%@<>=!;,()[]{}";
    T[] t = {
      T.PLUS, T.MINUS, T.STAR, T.SLASH, T.PERCENT, T.AT, T.LT, T.GT, T.EQ, T.NOT, T.SEMI, T.COMMA, T.LP,
      T.RP, T.LS, T.RS, T.LB, T.RB
    };
    for (int i = 0; i < o.length(); i++) CT[o.charAt(i)] = t[i];
    CT2['<'] = T.LE;
    CT2['>'] = T.GE;
    CT2['='] = T.EQEQ;
    CT2['!'] = T.NE;
  }

  public static class Token {
    public final T type;
    /** Original token text. Keeping slices as strings keeps later stages simple. */
    public final String text;

    Token(T y, String s) {
      type = y;
      text = s;
    }

    /** Returns a compact debug representation used in parser diagnostics/debugging. */
    public String toString() {
      return type + " '" + text + "'";
    }
  }

  final String s;
  final int n;
  int p;

  public Lexer(String s, String fn) {
    this.s = s;
    n = s.length();
  }

  /** Returns the current character, or {@code 0} when the cursor is at EOF. */
  char ch() {
    return p < n ? s.charAt(p) : 0;
  }

  /** Returns the character {@code d} positions ahead, or {@code 0} past EOF. */
  char ch(int d) {
    int i = p + d;
    return i < n ? s.charAt(i) : 0;
  }

  /** Consumes and returns the current character. */
  char adv() {
    return s.charAt(p++);
  }

  /**
   * Tokenizes the full input.
   *
   * <p>Whitespace and comments are skipped here so the parser only sees syntactically meaningful
   * tokens. Any unrecognized byte immediately becomes a hard error instead of trying to recover.
   */
  public List<Token> tokenize() {
    p = 0;
    List<Token> r = new ArrayList<>();
    while (p < n) {
      char c = ch();
      if (c <= ' ') {
        adv();
        continue;
      }
      // Skip C++-style line comments.
      if (c == '/' && ch(1) == '/') {
        while (p < n && ch() != '\n') adv();
        continue;
      }
      // Skip C-style block comments.
      if (c == '/' && ch(1) == '*') {
        adv();
        adv();
        while (p < n) {
          if (ch() == '*' && ch(1) == '/') {
            adv();
            adv();
            break;
          }
          adv();
        }
        continue;
      }
      // Scan an identifier first, then classify it as a keyword if present in KW.
      if (c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c == '_') {
        StringBuilder sb = new StringBuilder();
        while (ch() >= 'a' && ch() <= 'z'
            || ch() >= 'A' && ch() <= 'Z'
            || ch() >= '0' && ch() <= '9'
            || ch() == '_') sb.append(adv());
        String w = sb.toString();
        r.add(new Token(KW.getOrDefault(w, T.IDENT), w));
      } else if (c >= '0' && c <= '9' || c == '.' && ch(1) >= '0' && ch(1) <= '9') {
        r.add(num());
      } else if ((c == '<' || c == '>' || c == '=' || c == '!') && ch(1) == '=') {
        String tx = s.substring(p, p + 2);
        p += 2;
        r.add(new Token(CT2[c], tx));
      } else if (c == '&' && ch(1) == '&') {
        p += 2;
        r.add(new Token(T.AND, "&&"));
      } else if (c == '|' && ch(1) == '|') {
        p += 2;
        r.add(new Token(T.OR, "||"));
      } else if (c < 128 && CT[c] != null) {
        adv();
        r.add(new Token(CT[c], "" + c));
      } else throw new RuntimeException("Unexpected: " + c);
    }
    r.add(new Token(T.EOF, ""));
    return r;
  }

  /**
   * Scans a numeric literal.
   *
   * <p>This accepts the lexical forms needed by the project, including hex, decimal, fractional,
   * exponent, and suffix forms. The exact semantic interpretation is deferred to later stages.
   */
  Token num() {
    StringBuilder sb = new StringBuilder();
    if (ch() == '0' && (ch(1) == 'x' || ch(1) == 'X')) {
      sb.append(adv());
      sb.append(adv());
      while (hex(ch())) sb.append(adv());
      if (ch() == '.') {
        sb.append(adv());
        while (hex(ch())) sb.append(adv());
      }
      if (ch() == 'p' || ch() == 'P') {
        sb.append(adv());
        if (ch() == '+' || ch() == '-') sb.append(adv());
        while (ch() >= '0' && ch() <= '9') sb.append(adv());
      }
    } else {
      while (ch() >= '0' && ch() <= '9') sb.append(adv());
      if (ch() == '.') {
        sb.append(adv());
        while (ch() >= '0' && ch() <= '9') sb.append(adv());
      }
      if (ch() == 'e' || ch() == 'E') {
        sb.append(adv());
        if (ch() == '+' || ch() == '-') sb.append(adv());
        while (ch() >= '0' && ch() <= '9') sb.append(adv());
      }
    }
    while ("fFlLuU".indexOf(ch()) >= 0) sb.append(adv());
    return new Token(T.NUM, sb.toString());
  }

  /** Returns whether {@code c} is a hexadecimal digit. */
  boolean hex(char c) {
    return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F';
  }
}
