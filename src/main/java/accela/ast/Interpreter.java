package accela.ast;

import static accela.ast.Node.Op;
import static accela.ast.Node.Tag.*;

import java.io.*;
import java.util.*;

/**
 * A lightweight AST-level interpreter used as a testing tool.
 *
 * <p>In practice it is mainly useful for debugging AST and for test-side result comparison.
 *
 * <p>The implementation is intentionally not treated as core infrastructure, so this file should be
 * viewed as support code rather than a source of semantics.
 */
public class Interpreter {
  private static class ReturnException extends RuntimeException {
    final Object value;

    ReturnException(Object value) {
      this.value = value;
    }
  }

  private static class BreakException extends RuntimeException {}

  private static class ContinueException extends RuntimeException {}

  private static class Pointer {
    final Object base;
    final int offset;

    Pointer(Object base, int offset) {
      this.base = base;
      this.offset = offset;
    }

    Object get(int index) {
      if (base instanceof int[]) return ((int[]) base)[offset + index];
      if (base instanceof float[]) return ((float[]) base)[offset + index];
      return null;
    }

    void set(int index, Object value) {
      if (base instanceof int[]) ((int[]) base)[offset + index] = ((Number) value).intValue();
      else if (base instanceof float[])
        ((float[]) base)[offset + index] = ((Number) value).floatValue();
    }
  }

  private final Map<String, Object> globals = new HashMap<>();
  private final Deque<Map<String, Object>> locals = new ArrayDeque<>();
  private Object exitCode = null;
  private boolean inGlobalScope = true;

  private final InputStream in = System.in;
  private int peekedChar = -2;

  public Object getExitCode() {
    return exitCode;
  }

  public Interpreter() {
    locals.push(new HashMap<>());
  }

  private int readChar() {
    if (peekedChar != -2) {
      int c = peekedChar;
      peekedChar = -2;
      return c;
    }
    try {
      return in.read();
    } catch (IOException e) {
      return -1;
    }
  }

  private int peekChar() {
    if (peekedChar == -2)
      try {
        peekedChar = in.read();
      } catch (IOException e) {
        peekedChar = -1;
      }
    return peekedChar;
  }

  private void skipWhitespace() {
    while (peekChar() != -1 && Character.isWhitespace(peekChar())) readChar();
  }

  private String readToken() {
    skipWhitespace();
    StringBuilder sb = new StringBuilder();
    while (peekChar() != -1 && !Character.isWhitespace(peekChar())) sb.append((char) readChar());
    return sb.toString();
  }

  private int readInt() {
    String t = readToken();
    if (t.isEmpty()) return 0;
    try {
      if (t.startsWith("0x") || t.startsWith("0X")) return (int) Long.parseLong(t.substring(2), 16);
      if (t.startsWith("0") && t.length() > 1) return (int) Long.parseLong(t.substring(1), 8);
      return Integer.parseInt(t);
    } catch (Exception e) {
      return 0;
    }
  }

  private float readFloat() {
    String t = readToken();
    if (t.isEmpty()) return 0.0f;
    try {
      return Float.parseFloat(t);
    } catch (Exception e) {
      return 0.0f;
    }
  }

  /** Format float as C-style hex (%a): add '+' in exponent, strip trailing '.0' */
  private static String hexFloat(float f) {
    String s = Float.toHexString(f);
    s = s.replaceAll("\\.0+(p)", "$1");
    s = s.replaceAll("p([^+-])", "p+$1");
    return s;
  }

  private void enterScope() {
    locals.push(new HashMap<>());
  }

  private void exitScope() {
    locals.pop();
  }

  private void setVariable(String name, Object value) {
    for (Map<String, Object> env : locals)
      if (env.containsKey(name)) {
        env.put(name, value);
        return;
      }
    globals.put(name, value);
  }

  private Object getVariable(String name) {
    for (Map<String, Object> env : locals) if (env.containsKey(name)) return env.get(name);
    if (globals.containsKey(name)) return globals.get(name);
    throw new RuntimeException("Undefined variable: " + name);
  }

  private boolean isTrue(Object v) {
    if (v instanceof Boolean) return (Boolean) v;
    if (v instanceof Integer) return (Integer) v != 0;
    if (v instanceof Float) return (Float) v != 0.0f;
    return false;
  }

  /** Executes the translation unit by evaluating globals first and then calling {@code main}. */
  public void run(Node unit) {
    for (Node child : unit.kids) if (child.tag != FUNC) exec(child);
    inGlobalScope = false;
    Node main = null;
    for (Node child : unit.kids) if (child.tag == FUNC && "main".equals(child.s)) main = child;
    if (main != null) exitCode = callFunc(main, Collections.emptyList());
  }

  private void exec(Node n) {
    if (n == null) return;
    switch (n.tag) {
      case BLOCK:
        enterScope();
        try {
          for (Node s : n.kids) exec(s);
        } finally {
          exitScope();
        }
        break;
      case DECL_STMT:
        for (Node d : n.kids) exec(d);
        break;
      case VAR:
        execVar(n);
        break;
      case IF:
        if (isTrue(eval(n.kids.get(0)))) exec(n.kids.get(1));
        else if (n.kids.size() > 2) exec(n.kids.get(2));
        break;
      case WHILE:
        while (isTrue(eval(n.kids.get(0)))) {
          try {
            exec(n.kids.get(1));
          } catch (BreakException e) {
            break;
          } catch (ContinueException e) {
            continue;
          }
        }
        break;
      case RET:
        throw new ReturnException(n.kids.isEmpty() ? null : eval(n.kids.get(0)));
      case BREAK:
        throw new BreakException();
      case CONT:
        throw new ContinueException();
      default:
        eval(n);
        break;
    }
  }

  private void execVar(Node n) {
    Object val;
    if (n.ty.isArray()) {
      int size = n.ty.flatSize();
      Object base = n.ty.isFloat() ? new float[size] : new int[size];
      Pointer ptr = new Pointer(base, 0);
      if (!n.kids.isEmpty()) assignInitList(ptr, n.kids.get(0), n.ty.dims, 0);
      val = ptr;
    } else {
      if (!n.kids.isEmpty()) val = eval(n.kids.get(0));
      else val = n.ty.isFloat() ? (Object) 0.0f : (Object) 0;
    }
    if (inGlobalScope) globals.put(n.s, val);
    else locals.peek().put(n.s, val);
  }

  private void assignInitList(Pointer ptr, Node init, int[] dims, int level) {
    if (init.tag == INIT_LIST) {
      int subSize = 1;
      for (int i = level + 1; i < dims.length; i++) subSize *= dims[i];
      int offset = 0;
      for (Node item : init.kids) {
        if (item.tag == INIT_LIST) {
          if (offset % subSize != 0) offset = (offset / subSize + 1) * subSize;
          assignInitList(new Pointer(ptr.base, ptr.offset + offset), item, dims, level + 1);
          offset += subSize;
        } else {
          ptr.set(offset++, eval(item));
        }
      }
    } else {
      ptr.set(0, eval(init));
    }
  }

  private Object eval(Node n) {
    if (n == null) return null;
    switch (n.tag) {
      case LIT:
        return n.literal.asNumber();
      case REF:
        return getVariable(n.s);
      case BIN:
        return evalBinary(n);
      case UNARY:
        return evalUnary(n);
      case CALL:
        return evalCall(n);
      case SUB:
        return evalSubscript(n);
      case CAST:
        return evalCast(n);
      case INIT_LIST:
        return n;
      default:
        exec(n);
        return 0;
    }
  }

  private Object evalLiteral(String s) {
    String v = s.toLowerCase();
    if (v.contains(".") || v.contains("e") || v.contains("p")) return Float.parseFloat(v);
    try {
      if (v.startsWith("0x")) return (int) Long.parseLong(v.substring(2), 16);
      if (v.startsWith("0") && v.length() > 1) return (int) Long.parseLong(v.substring(1), 8);
      return Integer.parseInt(v);
    } catch (Exception e) {
      return (int) Long.parseLong(v);
    }
  }

  private Object evalBinary(Node n) {
    // Assignment
    if (n.op == Op.ASSIGN) {
      Object val = eval(n.kids.get(1));
      Node lhs = n.kids.get(0);
      if (lhs.tag == REF) setVariable(lhs.s, val);
      else if (lhs.tag == SUB) {
        // Reuse evalSubscript logic but for write: compute pointer + offset
        Pointer p = (Pointer) eval(lhs.kids.get(0));
        Ty arrTy = lhs.kids.get(0).ty;
        int offset = 0;
        for (int i = 1; i < lhs.kids.size(); i++) {
          int idx = ((Number) eval(lhs.kids.get(i))).intValue();
          if (arrTy != null && arrTy.isArray()) {
            Ty derefed = arrTy.deref();
            int stride = (derefed != null && derefed.isArray()) ? derefed.flatSize() : 1;
            offset += idx * stride;
            arrTy = derefed;
          }
        }
        p.set(offset, val);
      }
      return val;
    }
    // Additive chain optimization
    if (n.op == Op.ADD || n.op == Op.SUB) {
      List<Node> terms = new ArrayList<>();
      List<Op> ops = new ArrayList<>();
      Node curr = n;
      while (curr.tag == BIN && (curr.op == Op.ADD || curr.op == Op.SUB)) {
        terms.add(curr.kids.get(1));
        ops.add(curr.op);
        curr = curr.kids.get(0);
      }
      terms.add(curr);
      Object res = eval(terms.get(terms.size() - 1));
      if (res instanceof Pointer) res = ((Pointer) res).get(0);
      for (int i = terms.size() - 2; i >= 0; i--) {
        Object rhs = eval(terms.get(i));
        if (rhs instanceof Pointer) rhs = ((Pointer) rhs).get(0);
        if (res instanceof Integer && rhs instanceof Integer) {
          int l = (Integer) res, r = (Integer) rhs;
          res = ops.get(i) == Op.ADD ? l + r : l - r;
        } else {
          float l = ((Number) res).floatValue(), r = ((Number) rhs).floatValue();
          res = ops.get(i) == Op.ADD ? l + r : l - r;
        }
      }
      return res;
    }
    // Short-circuit logical
    Object l = eval(n.kids.get(0));
    if (n.op == Op.AND) return !isTrue(l) ? 0 : isTrue(eval(n.kids.get(1))) ? 1 : 0;
    if (n.op == Op.OR) return isTrue(l) ? 1 : isTrue(eval(n.kids.get(1))) ? 1 : 0;
    // Other binary
    Object r = eval(n.kids.get(1));
    if (l instanceof Pointer) l = ((Pointer) l).get(0);
    if (r instanceof Pointer) r = ((Pointer) r).get(0);
    if (l instanceof Integer && r instanceof Integer) {
      int lv = (Integer) l, rv = (Integer) r;
      switch (n.op) {
        case MUL:
          return lv * rv;
        case DIV:
          return lv / rv;
        case MOD:
          return lv % rv;
        case LT:
          return lv < rv ? 1 : 0;
        case GT:
          return lv > rv ? 1 : 0;
        case LE:
          return lv <= rv ? 1 : 0;
        case GE:
          return lv >= rv ? 1 : 0;
        case EQ:
          return lv == rv ? 1 : 0;
        case NE:
          return lv != rv ? 1 : 0;
        default:
          break;
      }
    } else {
      float lv = ((Number) l).floatValue(), rv = ((Number) r).floatValue();
      switch (n.op) {
        case MUL:
          return lv * rv;
        case DIV:
          return lv / rv;
        case LT:
          return lv < rv ? 1 : 0;
        case GT:
          return lv > rv ? 1 : 0;
        case LE:
          return lv <= rv ? 1 : 0;
        case GE:
          return lv >= rv ? 1 : 0;
        case EQ:
          return lv == rv ? 1 : 0;
        case NE:
          return lv != rv ? 1 : 0;
        default:
          break;
      }
    }
    return 0;
  }

  private Object evalUnary(Node n) {
    Object r = eval(n.kids.get(0));
    if (r instanceof Pointer) r = ((Pointer) r).get(0);
    if (r instanceof Integer) {
      int v = (Integer) r;
      switch (n.op) {
        case NEG:
          return -v;
        case POS:
          return v;
        case NOT:
          return v == 0 ? 1 : 0;
        default:
          break;
      }
    } else {
      float v = ((Number) r).floatValue();
      switch (n.op) {
        case NEG:
          return -v;
        case POS:
          return v;
        case NOT:
          return v == 0.0f ? 1 : 0;
        default:
          break;
      }
    }
    return 0;
  }

  private Object evalCall(Node n) {
    String name = n.kids.get(0).s;
    List<Object> args = new ArrayList<>();
    for (int i = 1; i < n.kids.size(); i++) args.add(eval(n.kids.get(i)));
    switch (name) {
      case "putint":
        System.out.print(((Number) args.get(0)).intValue());
        return 0;
      case "putch":
        System.out.print((char) ((Number) args.get(0)).intValue());
        return 0;
      case "putfloat":
        System.out.print(hexFloat(((Number) args.get(0)).floatValue()));
        return 0;
      case "getint":
        return readInt();
      case "getch":
        return readChar();
      case "getfloat":
        return readFloat();
      case "getarray":
        {
          Pointer p = (Pointer) args.get(0);
          int cnt = readInt();
          for (int i = 0; i < cnt; i++) p.set(i, readInt());
          return cnt;
        }
      case "getfarray":
        {
          Pointer p = (Pointer) args.get(0);
          int cnt = readInt();
          for (int i = 0; i < cnt; i++) p.set(i, readFloat());
          return cnt;
        }
      case "putarray":
        {
          int cnt = ((Number) args.get(0)).intValue();
          Pointer p = (Pointer) args.get(1);
          System.out.printf("%d:", cnt);
          for (int i = 0; i < cnt; i++) System.out.printf(" %d", ((Number) p.get(i)).intValue());
          System.out.println();
          return 0;
        }
      case "putfarray":
        {
          int cnt = ((Number) args.get(0)).intValue();
          Pointer p = (Pointer) args.get(1);
          System.out.printf("%d:", cnt);
          for (int i = 0; i < cnt; i++)
            System.out.printf(" %s", hexFloat(((Number) p.get(i)).floatValue()));
          System.out.println();
          return 0;
        }
      case "starttime":
      case "stoptime":
        return 0;
    }
    return callFunc(n.kids.get(0).decl, args);
  }

  private Object callFunc(Node func, List<Object> args) {
    Deque<Map<String, Object>> saved = new ArrayDeque<>(locals);
    locals.clear();
    enterScope();
    int nParams = func.kids.size() - 1; // body is last kid
    for (int i = 0; i < nParams; i++) locals.peek().put(func.kids.get(i).s, args.get(i));
    try {
      exec(func.kids.get(func.kids.size() - 1)); // body
      return 0;
    } catch (ReturnException e) {
      return e.value;
    } finally {
      exitScope();
      locals.clear();
      locals.addAll(saved);
    }
  }

  private Object evalSubscript(Node n) {
    Pointer p = (Pointer) eval(n.kids.get(0));
    // Flat subscript: kids=[ref, idx0, idx1, ...], compute linear offset from ref's dims
    Ty arrTy = n.kids.get(0).ty;
    int offset = 0;
    for (int i = 1; i < n.kids.size(); i++) {
      int idx = ((Number) eval(n.kids.get(i))).intValue();
      if (arrTy != null && arrTy.isArray()) {
        Ty derefed = arrTy.deref();
        int stride = (derefed != null && derefed.isArray()) ? derefed.flatSize() : 1;
        offset += idx * stride;
        arrTy = derefed;
      }
    }
    if (n.ty != null && n.ty.isArray()) return new Pointer(p.base, p.offset + offset);
    return p.get(offset);
  }

  private Object evalCast(Node n) {
    Object v = eval(n.kids.get(0));
    if (v instanceof Pointer) v = ((Pointer) v).get(0);
    if (n.ty.isInt()) return ((Number) v).intValue();
    return ((Number) v).floatValue();
  }
}
