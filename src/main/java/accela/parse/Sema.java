package accela.parse;

import static accela.ast.Node.Tag.*;

import accela.ast.Node;
import accela.ast.Node.Op;
import accela.ast.Node.Tag;
import accela.ast.Ty;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Sema: symbol binding, type inferencing, constant folding. */
// - TODO: Simple AST rewrite can be done here
// e.g.
// if (0) {dead code, remove}
// if (1) { A;B;C } -> A;B;C
// This would make IR graph simpler.
// - TODO: Extract info for super-optimization / poly
// Not sure we will actually use this, writing SMT Solver seems hard.
public class Sema {
  private SymbolTable scope = new SymbolTable(null);

  public Sema() {
    for (String[] b :
        new String[][] {
          {"getint", "int"},
          {"getch", "int"},
          {"getfloat", "float"},
          {"getarray", "int"},
          {"getfarray", "int"},
          {"putint", "void"},
          {"putch", "void"},
          {"putfloat", "void"},
          {"putarray", "void"},
          {"putfarray", "void"},
          {"starttime", "void"},
          {"stoptime", "void"}
        }) {
      Ty ty = b[1].equals("int") ? Ty.INT : b[1].equals("float") ? Ty.FLOAT : Ty.VOID;
      scope.put(b[0], new Node(Tag.FUNC, b[0], ty));
    }
  }

  public void analyze(Node unit) {
    for (Node child : unit.kids) {
      if (child.tag == FUNC) analyzeFuncDef(child);
      else if (child.tag == VAR) analyzeVarDecl(child);
      else if (child.tag == DECL_STMT) for (Node d : child.kids) analyzeVarDecl(d);
    }
  }

  private void analyzeFuncDef(Node f) {
    scope.put(f.s, f);
    enter();
    int nParams = f.kids.size() - 1;
    for (int i = 0; i < nParams; i++) {
      Node pv = f.kids.get(i);
      if (pv.dimExprs != null && (pv.flag || !pv.dimExprs.isEmpty())) {
        List<Integer> dims = new ArrayList<>();
        if (pv.flag) dims.add(0); // firstDimEmpty here
        for (Node dimExpr : pv.dimExprs) dims.add(evalConst(analyzeExpr(dimExpr)));
        pv.ty = Ty.array(pv.ty, dims.stream().mapToInt(Integer::intValue).toArray());
      }
      scope.put(pv.s, pv);
    }
    analyzeBlock(f.kids.get(f.kids.size() - 1));
    exit();
  }

  private void analyzeBlock(Node block) {
    enter();
    for (Node stmt : block.kids) analyzeStmt(stmt);
    exit();
  }

  private void analyzeStmt(Node n) {
    if (n == null) return;
    switch (n.tag) {
      case DECL_STMT:
        for (Node d : n.kids) analyzeVarDecl(d);
        break;
      case BLOCK:
        analyzeBlock(n);
        break;
      case IF:
        n.kids.set(0, analyzeExpr(n.kids.get(0)));
        analyzeStmt(n.kids.get(1));
        if (n.kids.size() > 2) analyzeStmt(n.kids.get(2));
        break;
      case WHILE:
        n.kids.set(0, analyzeExpr(n.kids.get(0)));
        analyzeStmt(n.kids.get(1));
        break;
      case RET:
        if (!n.kids.isEmpty()) n.kids.set(0, analyzeExpr(n.kids.get(0)));
        break;
      default:
        analyzeExpr(n);
        break;
    }
  }

  private void analyzeVarDecl(Node v) {
    if (v.dimExprs != null && !v.dimExprs.isEmpty()) {
      int[] dims = new int[v.dimExprs.size()];
      for (int i = 0; i < dims.length; i++) dims[i] = evalConst(analyzeExpr(v.dimExprs.get(i)));
      v.ty = Ty.array(v.ty, dims);
    }
    scope.put(v.s, v);
    if (!v.kids.isEmpty()) {
      Node init = v.kids.get(0);
      if (v.ty.isArray() && init.tag == INIT_LIST) {
        v.kids.set(0, flattenInit(v.ty, init));
      } else if (init.tag == INIT_LIST) {
        analyzeInitList(init);
      } else {
        v.kids.set(0, maybeCast(v.ty, analyzeExpr(init)));
      }
    }
    if (v.flag && !v.ty.isArray() && !v.kids.isEmpty() && v.kids.get(0).tag != LIT) {
      int val = evalConst(v.kids.get(0));
      Node lit = new Node(LIT, Integer.toString(val));
      lit.ty = v.ty;
      v.kids.set(0, lit);
    }
  }

  // flatten nested init lists, zero-fill, sub-aggregate alignment
  private Node flattenInit(Ty ty, Node raw) {
    int total = ty.flatSize();
    int[] dims = ty.dims;
    List<Node> flat = new ArrayList<>(total);
    for (int i = 0; i < total; i++) flat.add(new Node(LIT, "0"));
    fillRec(raw, flat, dims, 0, 0);
    for (int i = 0; i < flat.size(); i++) flat.set(i, analyzeExpr(flat.get(i)));
    return rebuildInit(flat, ty.elem, dims, 0, 0);
  }

  private int fillRec(Node raw, List<Node> flat, int[] dims, int level, int offset) {
    if (raw.tag != INIT_LIST) {
      if (offset < flat.size()) flat.set(offset, raw);
      return offset + 1;
    }
    int subSize = 1;
    for (int i = level + 1; i < dims.length; i++) subSize *= dims[i];
    int cur = offset;
    for (Node item : raw.kids) {
      if (item.tag == INIT_LIST) {
        if (cur % subSize != 0) cur = (cur / subSize + 1) * subSize;
        int start = cur;
        fillRec(item, flat, dims, level + 1, cur);
        cur = start + subSize;
      } else {
        cur = fillRec(item, flat, dims, dims.length, cur);
      }
    }
    return cur;
  }

  private Node rebuildInit(List<Node> flat, Ty elem, int[] dims, int level, int offset) {
    if (level == dims.length) return flat.get(offset);
    int[] subDims = Arrays.copyOfRange(dims, level, dims.length);
    Node res = new Node(INIT_LIST);
    res.ty = Ty.array(elem, subDims);
    int subSize = 1;
    for (int i = level + 1; i < dims.length; i++) subSize *= dims[i];
    for (int i = 0; i < dims[level]; i++)
      res.kids.add(rebuildInit(flat, elem, dims, level + 1, offset + i * subSize));
    return res;
  }

  private void analyzeInitList(Node il) {
    for (int i = 0; i < il.kids.size(); i++) {
      Node child = il.kids.get(i);
      if (child.tag == INIT_LIST) analyzeInitList(child);
      else il.kids.set(i, analyzeExpr(child));
    }
  }

  private Node analyzeExpr(Node n) {
    if (n == null) return null;
    switch (n.tag) {
      case BIN:
        return analyzeBinary(n);
      case UNARY:
        n.kids.set(0, analyzeExpr(n.kids.get(0)));
        return n;
      case CALL:
        return analyzeCall(n);
      case REF:
        Node decl = scope.get(n.s);
        if (decl == null) throw new RuntimeException("Undefined: " + n.s);
        n.decl = decl;
        n.ty = decl.ty;
        return n;
      case SUB:
        n.kids.set(0, analyzeExpr(n.kids.get(0)));
        n.kids.set(1, analyzeExpr(n.kids.get(1)));
        // flatten: SUB(SUB(ref,i),j) → SUB(ref,i,j)
        Node base = n.kids.get(0);
        if (base.tag == SUB) {
          Node idx = n.kids.get(1);
          n.kids.clear();
          n.kids.addAll(base.kids);
          n.kids.add(idx);
        }
        Ty baseTy = tyOf(n.kids.get(0));
        Ty cur = baseTy;
        for (int i = 1; i < n.kids.size(); i++) if (cur != null && cur.isArray()) cur = cur.deref();
        n.ty = cur;
        // Const array folding
        if (n.ty != null && !n.ty.isArray()) {
          Node ref = n.kids.get(0);
          if (ref.tag == REF
              && ref.decl != null
              && ref.decl.flag
              && !ref.decl.kids.isEmpty()
              && ref.decl.kids.get(0).tag == INIT_LIST) {
            if (allConst(n.kids, 1)) {
              Node leaf = constSubscript(ref.decl.kids.get(0), n.kids, 1);
              if (leaf != null) return leaf;
            }
          }
        }
        return n;
      case CAST:
        n.kids.set(0, analyzeExpr(n.kids.get(0)));
        return n;
      default:
        return n; // LIT
    }
  }

  private Node analyzeBinary(Node n) {
    List<Node> spine = new ArrayList<>();
    Node cur = n;
    while (cur.tag == BIN) {
      spine.add(cur);
      cur = cur.kids.get(0);
    }
    cur = analyzeExpr(cur);
    for (int i = spine.size() - 1; i >= 0; i--) {
      Node bin = spine.get(i);
      bin.kids.set(0, cur);
      bin.kids.set(1, analyzeExpr(bin.kids.get(1)));
      if (bin.op == Op.ASSIGN) {
        bin.ty = tyOf(bin.kids.get(0));
        bin.kids.set(1, maybeCast(bin.ty, bin.kids.get(1)));
      } else if (bin.op.isRelational() || bin.op.isLogical()) {
        bin.ty = Ty.INT;
      } else {
        Ty lt = tyOf(bin.kids.get(0)), rt = tyOf(bin.kids.get(1));
        if (lt.isFloat() || rt.isFloat()) {
          bin.ty = Ty.FLOAT;
          bin.kids.set(0, maybeCast(Ty.FLOAT, bin.kids.get(0)));
          bin.kids.set(1, maybeCast(Ty.FLOAT, bin.kids.get(1)));
        } else {
          bin.ty = Ty.INT;
        }
      }
      cur = bin;
    }
    return cur;
  }

  private Node analyzeCall(Node n) {
    Node ref = n.kids.get(0);
    Node decl = scope.get(ref.s);
    if (decl == null) throw new RuntimeException("Undefined function: " + ref.s);
    ref.decl = decl;
    ref.ty = decl.ty;
    n.ty = decl.ty;
    for (int i = 1; i < n.kids.size(); i++) n.kids.set(i, analyzeExpr(n.kids.get(i)));

    if (decl.tag == FUNC && !decl.kids.isEmpty()) {
      int nParams = decl.kids.size() - 1;
      int nArgs = n.kids.size() - 1;
      for (int i = 0; i < nArgs && i < nParams; i++)
        n.kids.set(i + 1, maybeCast(decl.kids.get(i).ty, n.kids.get(i + 1)));
    }
    return n;
  }

  private Ty tyOf(Node n) {
    if (n == null) return Ty.INT;
    Ty t = n.type();
    return t != null ? t : Ty.INT;
  }

  private Node maybeCast(Ty target, Node expr) {
    if (expr == null || target == null) return expr;
    Ty src = tyOf(expr);
    if (target.isInt() && src.isFloat()) {
      Node c = new Node(CAST);
      c.ty = Ty.INT;
      c.kids.add(expr);
      return c;
    }
    if (target.isFloat() && src.isInt()) {
      Node c = new Node(CAST);
      c.ty = Ty.FLOAT;
      c.kids.add(expr);
      return c;
    }
    return expr;
  }

  int evalConst(Node node) {
    if (node.tag == LIT) {
      String v = node.s;
      if (v.contains(".")
          || v.contains("e")
          || v.contains("E")
          || v.contains("p")
          || v.contains("P")) return (int) Double.parseDouble(v);
      if (v.startsWith("0x") || v.startsWith("0X")) return Integer.parseInt(v.substring(2), 16);
      if (v.startsWith("0") && v.length() > 1) return Integer.parseInt(v.substring(1), 8);
      return Integer.parseInt(v);
    }
    if (node.tag == BIN) {
      int l = evalConst(node.kids.get(0)), r = evalConst(node.kids.get(1));
      switch (node.op) {
        case ADD:
          return l + r;
        case SUB:
          return l - r;
        case MUL:
          return l * r;
        case DIV:
          return l / r;
        case MOD:
          return l % r;
        case EQ:
          return l == r ? 1 : 0;
        case NE:
          return l != r ? 1 : 0;
        case LT:
          return l < r ? 1 : 0;
        case GT:
          return l > r ? 1 : 0;
        case LE:
          return l <= r ? 1 : 0;
        case GE:
          return l >= r ? 1 : 0;
        case AND:
          return (l != 0 && r != 0) ? 1 : 0;
        case OR:
          return (l != 0 || r != 0) ? 1 : 0;
        default:
          break;
      }
    }
    if (node.tag == UNARY) {
      int r = evalConst(node.kids.get(0));
      if (node.op == Op.NEG) return -r;
      if (node.op == Op.POS) return r;
    }
    if (node.tag == REF) {
      // Follow decl binding to resolve const values
      if (node.decl != null && node.decl.flag && !node.decl.kids.isEmpty())
        return evalConst(node.decl.kids.get(0));
    }
    if (node.tag == CAST) return evalConst(node.kids.get(0));
    if (node.tag == SUB) {
      Node ref = node.kids.get(0);
      if (ref.tag == REF
          && ref.decl != null
          && ref.decl.flag
          && !ref.decl.kids.isEmpty()
          && ref.decl.kids.get(0).tag == INIT_LIST) {
        Node leaf = constSubscript(ref.decl.kids.get(0), node.kids, 1);
        if (leaf != null) return evalConst(leaf);
      }
    }
    throw new RuntimeException("Constant expected: " + node.tag);
  }

  /** Check if all index kids are compile-time constants. */
  private boolean allConst(List<Node> kids, int startIdx) {
    for (int i = startIdx; i < kids.size(); i++) {
      try {
        evalConst(kids.get(i));
      } catch (RuntimeException e) {
        return false;
      }
    }
    return true;
  }

  private Node constSubscript(Node initList, List<Node> kids, int startIdx) {
    Node cur = initList;
    for (int i = startIdx; i < kids.size(); i++) {
      int idx = evalConst(kids.get(i));
      if (cur.tag != INIT_LIST || idx < 0 || idx >= cur.kids.size()) return null;
      cur = cur.kids.get(idx);
    }
    return cur;
  }

  private void enter() {
    scope = new SymbolTable(scope);
  }

  private void exit() {
    scope = scope.getParent();
  }
}
