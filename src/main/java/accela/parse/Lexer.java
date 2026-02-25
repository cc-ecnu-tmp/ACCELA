package accela.parse;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class Lexer {
  // Type, don't change the order of this list
  // THINGS CAN GET TERRIBLY WRONG IF YOU DO THIS!
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

  static final Map<String, T> KW = new HashMap<>(); // keywords

  // KW.put("int", T.INT); KW.put("void", T.VOID); simplicity.
  static {
    for (int i = 0; i <= T.FLOAT.ordinal(); i++) {
      T t = T.values()[i];
      KW.put(t.name().toLowerCase(), t);
    }
  }

  static final T[] CT = new T[128]; // single-char ops
  static final T[] CT2 = new T[128]; // double-char ops (<=,==,=>...)

  // O(1) mapping
  static {
    String o = "+-*/%<>=!;,()[]{}";
    T[] t = {
      T.PLUS, T.MINUS, T.STAR, T.SLASH, T.PERCENT, T.LT, T.GT, T.EQ, T.NOT, T.SEMI, T.COMMA, T.LP,
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
    public final String text; // TODO: replace with (start,end) offsets into source

    Token(T y, String s) {
      type = y;
      text = s;
    }

    // pretty print.
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

  // current char
  char ch() {
    return p < n ? s.charAt(p) : 0;
  }

  // look ahead by `d`
  char ch(int d) {
    int i = p + d;
    return i < n ? s.charAt(i) : 0;
  }

  // advance
  char adv() {
    return s.charAt(p++);
  }

  public List<Token> tokenize() {
    p = 0;
    List<Token> r = new ArrayList<>();
    while (p < n) {
      char c = ch();
      if (c <= ' ') {
        adv();
        continue;
      }
      // single-line comments
      if (c == '/' && ch(1) == '/') {
        while (p < n && ch() != '\n') adv();
        continue;
      }
      // multi-line comments
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
      // identifier or keyword
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

  // number
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

  boolean hex(char c) {
    return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F';
  }
}
