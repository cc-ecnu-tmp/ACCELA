package accela.parse;

import static accela.ast.Node.Tag.*;

import accela.ast.Node;
import accela.ast.Node.Op;
import accela.ast.Node.Tag;
import accela.ast.Ty;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Semantic analysis and frontend normalization pass.
 *
 * <p>This stage does more than classic "type checking":
 *
 * <p>- binds each reference/call to its declaration via the symbol table
 *
 * <p>- computes source-level types for expressions
 *
 * <p>- inserts explicit casts for int/float conversions
 *
 * <p>- evaluates compile-time constants where the frontend needs them
 *
 * <p>- reshapes a few AST forms into easier-to-lower representations, especially array initializers
 * and chained subscripts
 *
 * <p>The rest of the pipeline depends heavily on this normalization. In particular,
 * AST2IR assumes many of these rewrites have already happened.
 */
public class Sema {
  private static final int DEFAULT_VECTOR_LANES = 4;
  private SymbolTable scope = new SymbolTable(null);
  private final ConstEvaluator constEvaluator = new ConstEvaluator();

  /** Seeds the outermost scope with builtin runtime function declarations. */
  public Sema() {
    registerBuiltin("getint", Ty.INT);
    registerBuiltin("getch", Ty.INT);
    registerBuiltin("getfloat", Ty.FLOAT);
    registerBuiltin("getarray", Ty.INT);
    registerBuiltin("getfarray", Ty.INT);
    registerBuiltin("putint", Ty.VOID);
    registerBuiltin("putch", Ty.VOID);
    registerBuiltin("putfloat", Ty.VOID);
    registerBuiltin("putarray", Ty.VOID);
    registerBuiltin("putfarray", Ty.VOID);
    registerBuiltin("starttime", Ty.VOID);
    registerBuiltin("stoptime", Ty.VOID);
  }

  private void registerBuiltin(String name, Ty returnType) {
    scope.put(name, new Node(Tag.FUNC, name, returnType));
  }

  /** Analyzes the whole translation unit in source order. */
  public void analyze(Node unit) {
    for (Node child : unit.kids) {
      if (child.tag == FUNC) analyzeFuncDef(child);
      else if (child.tag == VAR) analyzeVarDecl(child);
      else if (child.tag == DECL_STMT) for (Node d : child.kids) analyzeVarDecl(d);
    }
  }

  /**
   * Analyzes a function definition.
   *
   * <p>Parameter types may still contain raw dimension expressions from parsing, so this method
   * resolves them before entering the function body.
   */
  private void analyzeFuncDef(Node f) {
    scope.put(f.s, f);
    enter();
    int nParams = f.kids.size() - 1;
    for (int i = 0; i < nParams; i++) {
      Node pv = f.kids.get(i);
      pv.ty = resolveInferredVector(pv.ty, null);
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

  /** Analyzes a lexical block with a fresh nested scope. */
  private void analyzeBlock(Node block) {
    enter();
    for (Node stmt : block.kids) analyzeStmt(stmt);
    exit();
  }

  /** Analyzes one statement and recursively rewrites any nested expressions. */
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
        n.kids.set(0, analyzeScalarCondition(n.kids.get(0)));
        analyzeStmt(n.kids.get(1));
        if (n.kids.size() > 2) analyzeStmt(n.kids.get(2));
        break;
      case WHILE:
        n.kids.set(0, analyzeScalarCondition(n.kids.get(0)));
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

  private Node analyzeScalarCondition(Node condition) {
    Node analyzed = analyzeExpr(condition);
    if (tyOf(analyzed).isVector())
      throw new RuntimeException("Vector values cannot be used directly as conditions");
    return analyzed;
  }

  /**
   * Analyzes a variable declaration and its initializer.
   *
   * <p>Array declarations are a notable special case: dimension expressions are folded to concrete
   * sizes, and brace initializers are normalized into a full tree with zero-filled omissions so
   * later passes do not need to re-implement C-style aggregate initialization rules.
   */
  private void analyzeVarDecl(Node v) {
    if (v.dimExprs != null && !v.dimExprs.isEmpty()) {
      int[] dims = new int[v.dimExprs.size()];
      for (int i = 0; i < dims.length; i++) dims[i] = evalConst(analyzeExpr(v.dimExprs.get(i)));
      v.ty = Ty.array(v.ty, dims);
    }
    v.ty = resolveInferredVector(v.ty, v.kids.isEmpty() ? null : v.kids.get(0));
    scope.put(v.s, v);
    if (!v.kids.isEmpty()) {
      Node init = v.kids.get(0);
      if (v.ty.isArray() && init.tag == INIT_LIST && !init.kids.isEmpty()) {
        v.kids.set(0, flattenInit(v.ty, init));
      } else if (v.ty.isVector() && init.tag == INIT_LIST) {
        v.kids.set(0, normalizeVectorInit(v.ty, init));
      } else if (init.tag == INIT_LIST) {
        analyzeInitList(init);
      } else {
        v.kids.set(0, maybeCast(v.ty, analyzeExpr(init)));
      }
    }
    if (v.flag && !v.ty.isArray() && !v.ty.isVector()
        && !v.kids.isEmpty() && v.kids.get(0).tag != LIT) {
      Node lit;
      if (v.ty.isFloat()) {
        lit = Node.floatLiteral(evalConstFloat(v.kids.get(0)));
      } else {
        lit = Node.intLiteral(evalConst(v.kids.get(0)));
      }
      lit.ty = v.ty;
      v.kids.set(0, lit);
    }
  }

  /** Resolves contextual vectors from brace width, falling back to four lanes when unavailable. */
  private Ty resolveInferredVector(Ty ty, Node init) {
    if (ty.isVector() && ty.lanes == 0) {
      return Ty.vector(ty.elem, inferVectorLanes(init, 0));
    }
    if (ty.isArray() && ty.elem.isVector() && ty.elem.lanes == 0) {
      return Ty.array(Ty.vector(ty.elem.elem, inferVectorLanes(init, ty.dims.length)), ty.dims);
    }
    return ty;
  }

  private int inferVectorLanes(Node init, int arrayDepth) {
    int inferred = inferVectorLanesOrZero(init, arrayDepth);
    return inferred == 0 ? DEFAULT_VECTOR_LANES : inferred;
  }

  private int inferVectorLanesOrZero(Node init, int arrayDepth) {
    if (init == null || init.tag != INIT_LIST || init.kids.isEmpty()) return 0;
    if (arrayDepth == 0) return init.kids.size();
    int lanes = 0;
    for (Node child : init.kids) {
      if (child.tag == INIT_LIST) {
        lanes = Math.max(lanes, inferVectorLanesOrZero(child, arrayDepth - 1));
      }
    }
    return lanes;
  }

  /** Type-checks a vector initializer and fills omitted lanes with zero. */
  private Node normalizeVectorInit(Ty vectorTy, Node raw) {
    if (raw.kids.size() > vectorTy.lanes)
      throw new RuntimeException("Too many vector initializer elements for " + vectorTy);
    Node normalized = new Node(INIT_LIST);
    normalized.ty = vectorTy;
    for (Node child : raw.kids) {
      if (child.tag == INIT_LIST) throw new RuntimeException("Nested vector initializer is invalid");
      normalized.kids.add(maybeCast(vectorTy.elem, analyzeExpr(child)));
    }
    while (normalized.kids.size() < vectorTy.lanes) {
      Node zero = vectorTy.elem == Ty.FLOAT ? Node.floatLiteral(0.0f) : Node.intLiteral(0);
      normalized.kids.add(zero);
    }
    return normalized;
  }

  /**
   * Normalizes a raw array initializer list.
   *
   * <p>The algorithm is:
   *
   * <p>1. allocate a flat buffer for all elements and prefill it with zeros
   *
   * <p>2. walk the raw brace tree, placing items into the correct sub-aggregate-aligned positions
   *
   * <p>3. analyze/cast each scalar element
   *
   * <p>4. rebuild a fully explicit nested {@code INIT_LIST} tree
   *
   * <p>This is not full SROA; it is frontend initializer normalization so later stages can treat
   * aggregates deterministically.
   */
  private Node flattenInit(Ty ty, Node raw) {
    int total = ty.flatSize();
    Ty leafType = ty.elem.isVector() ? ty.elem.elem : ty.elem;
    int[] dims = ty.elem.isVector()
        ? appendDimension(ty.dims, ty.elem.lanes) : ty.dims;
    List<Node> flat = new ArrayList<>(total);
    for (int i = 0; i < total; i++) {
      flat.add(leafType == Ty.FLOAT ? Node.floatLiteral(0.0f) : Node.intLiteral(0));
    }
    fillRec(raw, flat, dims, 0, 0);
    for (int i = 0; i < flat.size(); i++) {
      flat.set(i, maybeCast(leafType, analyzeExpr(flat.get(i))));
    }
    return ty.elem.isVector()
        ? rebuildVectorArrayInit(flat, ty, 0, 0)
        : rebuildInit(flat, ty.elem, dims, 0, 0);
  }

  private static int[] appendDimension(int[] dimensions, int extent) {
    int[] result = Arrays.copyOf(dimensions, dimensions.length + 1);
    result[dimensions.length] = extent;
    return result;
  }

  /** Rebuilds array structure while keeping each fixed vector as one aggregate leaf. */
  private Node rebuildVectorArrayInit(List<Node> flat, Ty arrayTy, int level, int offset) {
    if (level == arrayTy.dims.length) {
      Node vector = new Node(INIT_LIST);
      vector.ty = arrayTy.elem;
      for (int lane = 0; lane < arrayTy.elem.lanes; lane++) {
        vector.kids.add(flat.get(offset + lane));
      }
      return vector;
    }
    Node result = new Node(INIT_LIST);
    result.ty = Ty.array(
        arrayTy.elem, Arrays.copyOfRange(arrayTy.dims, level, arrayTy.dims.length));
    int stride = arrayTy.elem.lanes;
    for (int dimension = level + 1; dimension < arrayTy.dims.length; dimension++) {
      stride *= arrayTy.dims[dimension];
    }
    for (int index = 0; index < arrayTy.dims[level]; index++) {
      result.kids.add(rebuildVectorArrayInit(flat, arrayTy, level + 1, offset + index * stride));
    }
    return result;
  }

  /**
   * Recursive worker for {@link #flattenInit(Ty, Node)}.
   *
   * <p>{@code cur} advances through the flattened storage order, and nested brace groups are aligned
   * to sub-aggregate boundaries to match C/SysY initializer semantics.
   */
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

  /** Rebuilds a nested initializer tree from the flattened element buffer. */
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

  /** Recursively analyzes a non-array initializer list in place. */
  private void analyzeInitList(Node il) {
    for (int i = 0; i < il.kids.size(); i++) {
      Node child = il.kids.get(i);
      if (child.tag == INIT_LIST) analyzeInitList(child);
      else il.kids.set(i, analyzeExpr(child));
    }
  }

  /**
   * Analyzes one expression node and returns the normalized result node.
   *
   * <p>This pass is allowed to rewrite the tree shape, not just annotate it. For example, chained
   * subscripts are flattened and constant array accesses may collapse all the way to literals.
   */
  private Node analyzeExpr(Node n) {
    if (n == null) return null;
    switch (n.tag) {
      case BIN:
        return analyzeBinary(n);
      case UNARY:
        n.kids.set(0, analyzeExpr(n.kids.get(0)));
        if (n.op == Op.NOT) {
          if (tyOf(n.kids.get(0)).isVector())
            throw new RuntimeException("Vector logical negation is not defined");
          n.ty = Ty.INT;
        }
        else n.ty = tyOf(n.kids.get(0));
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
        n.kids.set(1, maybeCast(Ty.INT, analyzeExpr(n.kids.get(1))));
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
        for (int i = 1; i < n.kids.size(); i++) {
          if (cur == null || (!cur.isArray() && !cur.isVector())) {
            throw new RuntimeException("Cannot subscript non-aggregate type " + cur);
          }
          cur = cur.deref();
        }
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

  /**
   * Analyzes left-associative binary chains from the bottom up.
   *
   * <p>Walking the left spine first lets us propagate rewritten/type-adjusted children back into the
   * chain without deep recursion. The method also decides the resulting expression type and inserts
   * casts where mixed int/float arithmetic requires it.
   */
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
      } else if (bin.op.isLogical()) {
        if (tyOf(bin.kids.get(0)).isVector() || tyOf(bin.kids.get(1)).isVector())
          throw new RuntimeException("Vector && and || are not defined");
        bin.ty = Ty.INT;
      } else if (bin.op.isRelational()) {
        Ty operandTy = commonNumericType(bin.kids.get(0), bin.kids.get(1));
        bin.kids.set(0, maybeCast(operandTy, bin.kids.get(0)));
        bin.kids.set(1, maybeCast(operandTy, bin.kids.get(1)));
        bin.ty = operandTy.isVector() ? Ty.vector(Ty.INT, operandTy.lanes) : Ty.INT;
      } else {
        bin.ty = commonNumericType(bin.kids.get(0), bin.kids.get(1));
        if (bin.op == Op.MOD && bin.ty.isFloat())
          throw new RuntimeException("Remainder requires integer operands");
        bin.kids.set(0, maybeCast(bin.ty, bin.kids.get(0)));
        bin.kids.set(1, maybeCast(bin.ty, bin.kids.get(1)));
      }
      cur = bin;
    }
    return cur;
  }

  /** Computes APL-style scalar extension plus fixed-vector numeric promotion. */
  private Ty commonNumericType(Node left, Node right) {
    Ty lt = tyOf(left), rt = tyOf(right);
    boolean vector = lt.isVector() || rt.isVector();
    // SysY scalar literals keep their lexical type: `1. / 2.` is floating-point division.
    // The integral-float compatibility rule exists only for vector scalar-extension (for example,
    // `int4 + 1.0`) and must not leak into ordinary scalar arithmetic.
    if (vector) {
      lt = effectiveVectorNumericType(left);
      rt = effectiveVectorNumericType(right);
    }
    Ty elem = lt.isFloat() || rt.isFloat() ? Ty.FLOAT : Ty.INT;
    if (!vector) return elem;
    int lanes = Math.max(lt.isVector() ? lt.lanes : 1, rt.isVector() ? rt.lanes : 1);
    return Ty.vector(elem, lanes);
  }

  /** Integral float literals retain integer behavior when mixed with vectors. */
  private Ty effectiveVectorNumericType(Node node) {
    Ty type = tyOf(node);
    if (node.tag != LIT || !node.literal.isFloat()) return type;
    float value = node.literal.asFloat();
    if (Float.isFinite(value) && value >= Integer.MIN_VALUE && value <= Integer.MAX_VALUE
        && value == (int) value && Float.floatToRawIntBits(value) != 0x80000000) return Ty.INT;
    return type;
  }

  /**
   * Resolves a direct call and adjusts arguments to declared parameter types when known.
   *
   * <p>The call node itself gets the callee's return type so later passes do not need to inspect
   * the declaration again just to know the result type.
   */
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

  /** Returns the semantic type of a node, defaulting conservatively to int. */
  private Ty tyOf(Node n) {
    if (n == null) return Ty.INT;
    Ty t = n.type();
    return t != null ? t : Ty.INT;
  }

  /** Inserts an explicit cast node when source and target scalar types differ. */
  private Node maybeCast(Ty target, Node expr) {
    if (expr == null || target == null) return expr;
    Ty src = tyOf(expr);
    if (target.equals(src)) return expr;
    if (target.isVector() || src.isVector()) {
      if (!target.isVector())
        throw new RuntimeException("Cannot implicitly convert vector " + src + " to " + target);
      Node c = new Node(CAST);
      c.ty = target;
      c.kids.add(expr);
      return c;
    }
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

  /**
   * Evaluates an expression as a compile-time integer constant.
   *
   * <p>This is used for array dimensions, const initializers, and some const-array indexing cases.
   * The routine follows declaration bindings, so {@code const int x = 3;} can participate in later
   * constant expressions through references to {@code x}.
   */
  int evalConst(Node node) {
    return constEvaluator.evaluateRequired(node).asInt();
  }

  /**
   * Evaluates an expression as a compile-time float constant.
   *
   * <p>This is used for folding scalar {@code const float} initializers without accidentally routing
   * them through integer semantics.
   */
  private float evalConstFloat(Node node) {
    return constEvaluator.evaluateRequired(node).asFloat();
  }

  /** Check if all index kids are compile-time constants. */
  private boolean allConst(List<Node> kids, int startIdx) {
    for (int i = startIdx; i < kids.size(); i++) {
      if (constEvaluator.evaluate(kids.get(i)).isEmpty()) return false;
    }
    return true;
  }

  /** Follows constant array initializer structure using compile-time indices. */
  private Node constSubscript(Node initList, List<Node> kids, int startIdx) {
    Node cur = initList;
    for (int i = startIdx; i < kids.size(); i++) {
      int idx = evalConst(kids.get(i));
      if (cur.tag != INIT_LIST || idx < 0 || idx >= cur.kids.size()) return null;
      cur = cur.kids.get(idx);
    }
    return cur;
  }

  /** Pushes a fresh nested scope. */
  private void enter() {
    scope = new SymbolTable(scope);
  }

  /** Pops the current scope. */
  private void exit() {
    scope = scope.getParent();
  }
}
