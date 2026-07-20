package accela.ir;

import static accela.ast.Node.Tag.*;

import accela.ast.LiteralValue;
import accela.ast.Node;
import accela.ast.Node.Op;
import accela.ast.Ty;
import accela.parse.ConstEvaluator;
import accela.pass.ir.transform.Mem2Reg;
import accela.ir.Instruction.Opcode;
import java.util.*;

/**
 * Converts a Sema-analyzed AST into a structured IR Module.
 *
 * <p>This pass is the bridge between frontend semantics and the project IR:
 *
 * <p>- source-language names and types are lowered into {@link Value}, {@link Type}, and
 * {@link Instruction} objects
 *
 * <p>- structured control flow becomes explicit basic blocks and branches
 *
 * <p>- local variables are initially represented with {@code alloca + load/store}; later passes
 * such as {@link Mem2Reg} are responsible for SSA promotion
 *
 * <p>The implementation intentionally relies on a Sema-normalized AST so later lowering logic can
 * stay relatively direct.
 */
public class AST2IR {
  private Module module;
  private Function curFunc;
  private BasicBlock entryBB;
  private IRBuilder b; // builder
  private final ConstEvaluator constEvaluator = new ConstEvaluator();

  private final Deque<Map<String, Value>> scopeStack = new ArrayDeque<>();
  private final Deque<Map<String, Type>> typeStack = new ArrayDeque<>();
  private final Deque<Map<String, Node>> nodeStack = new ArrayDeque<>();
  private final Map<String, Type> globalTypes = new LinkedHashMap<>();
  private final Map<String, GlobalVariable> globalVars = new LinkedHashMap<>();
  private final Deque<BasicBlock> breakTargets = new ArrayDeque<>();
  private final Deque<BasicBlock> contTargets = new ArrayDeque<>();
  private final Map<String, Function> funcRegistry = new LinkedHashMap<>();

  private static final Map<String, Type[]> RUNTIME_PARAMS = new HashMap<>();

  static {
    RUNTIME_PARAMS.put("getint", new Type[] {});
    RUNTIME_PARAMS.put("getch", new Type[] {});
    RUNTIME_PARAMS.put("getfloat", new Type[] {});
    RUNTIME_PARAMS.put("getarray", new Type[] {Type.PTR});
    RUNTIME_PARAMS.put("getfarray", new Type[] {Type.PTR});
    RUNTIME_PARAMS.put("putint", new Type[] {Type.INT});
    RUNTIME_PARAMS.put("putch", new Type[] {Type.INT});
    RUNTIME_PARAMS.put("putfloat", new Type[] {Type.FLOAT});
    RUNTIME_PARAMS.put("putarray", new Type[] {Type.INT, Type.PTR});
    RUNTIME_PARAMS.put("putfarray", new Type[] {Type.INT, Type.PTR});
    RUNTIME_PARAMS.put("_sysy_starttime", new Type[] {Type.INT});
    RUNTIME_PARAMS.put("_sysy_stoptime", new Type[] {Type.INT});
    RUNTIME_PARAMS.put("starttime", new Type[] {});
    RUNTIME_PARAMS.put("stoptime", new Type[] {});
  }

  private int labelCounter = 0;

  /**
   * Main lowering entry.
   *
   * <p>The top level is handled in two phases: globals first, then functions. That guarantees every
   * function body sees the complete global environment and all builtin declarations.
   *
   * <p>This pass deliberately lowers locals into a memory-oriented form ({@code alloca + load/store})
   * instead of building SSA directly. The frontend stays simple, while {@link Mem2Reg} recovers SSA
   * later for the promotable cases.
   */
  public Module convert(Node unit) {
    module = new Module();
    b = new IRBuilder();
    addRuntimeDecls();
    enterScope();

    for (Node child : unit.kids) {
      if (child.tag == Node.Tag.VAR) registerGlobal(child);
    }

    for (Node child : unit.kids) {
      if (child.tag == Node.Tag.FUNC) emitFunction(child);
    }

    exitScope();
    return module;
  }

  private void addRuntimeDecls() {
    declareRuntime("getint", Type.INT);
    declareRuntime("getch", Type.INT);
    declareRuntime("getfloat", Type.FLOAT);
    declareRuntime("getarray", Type.INT, Type.PTR);
    declareRuntime("getfarray", Type.INT, Type.PTR);
    declareRuntime("putint", Type.VOID, Type.INT);
    declareRuntime("putch", Type.VOID, Type.INT);
    declareRuntime("putfloat", Type.VOID, Type.FLOAT);
    declareRuntime("putarray", Type.VOID, Type.INT, Type.PTR);
    declareRuntime("putfarray", Type.VOID, Type.INT, Type.PTR);
    declareRuntime("_sysy_starttime", Type.VOID, Type.INT);
    declareRuntime("_sysy_stoptime", Type.VOID, Type.INT);
  }

  private void declareRuntime(String name, Type retType, Type... paramTypes) {
    Function f = new Function(name, retType);
    for (int i = 0; i < paramTypes.length; i++) {
      f.addArgument(paramTypes[i], "%p" + i);
    }
    module.addDeclare(f);
    funcRegistry.put(name, f);
  }

  private void enterScope() {
    scopeStack.push(new HashMap<>());
    typeStack.push(new HashMap<>());
    nodeStack.push(new HashMap<>());
  }

  private void exitScope() {
    scopeStack.pop();
    typeStack.pop();
    nodeStack.pop();
  }

  private void putVar(String name, Value reg, Type type) {
    scopeStack.peek().put(name, reg);
    typeStack.peek().put(name, type);
  }

  private void putVarNode(String name, Node node) {
    nodeStack.peek().put(name, node);
  }

  private Value lookupVar(String name) {
    for (Map<String, Value> scope : scopeStack)
      if (scope.containsKey(name)) return scope.get(name);
    return globalVars.get(name);
  }

  private Type lookupType(String name) {
    for (Map<String, Type> scope : typeStack)
      if (scope.containsKey(name)) return scope.get(name);
    return globalTypes.get(name);
  }

  private String nextLabel(String prefix) {
    return prefix + "." + (labelCounter++);
  }

  private BasicBlock addBlock(String label) {
    return curFunc.addBlock(label);
  }

  private void switchTo(BasicBlock bb) {
    b.setInsertPoint(bb);
  }

  /** Ensure current block is terminated, then switch to newBB. */
  private void brAndSwitch(BasicBlock newBB) {
    if (!b.isTerminated()) b.createBr(newBB);
    switchTo(newBB);
  }

  /**
   * Lowers one global declaration into a {@link GlobalVariable}.
   *
   * <p>Unlike local initialization, global initialization cannot emit executable IR, so the whole
   * initializer must be represented as a constant tree immediately.
   */
  private void registerGlobal(Node v) {
    Type irType = Type.fromSysY(v.ty);
    globalTypes.put(v.s, irType);

    Constant init;
    if (v.kids.isEmpty()) {
      init = Constant.zero(irType);
    } else {
      init = buildGlobalInit(v.kids.get(0), irType);
    }

    GlobalVariable gv = new GlobalVariable(v.s, irType, init, v.flag);
    module.addGlobal(gv);
    globalVars.put(v.s, gv);
  }

  /**
   * Converts a global initializer AST into a constant IR object.
   *
   * <p>Scalars are folded immediately. Arrays are expanded recursively to match the final aggregate
   * layout expected by the IR printer and backend.
   */
  private Constant buildGlobalInit(Node initNode, Type type) {
    if (type.isArray()) {
      if (initNode.tag == INIT_LIST) return buildGlobalArrayInit(initNode, type);
      return Constant.zero(type);
    }
    LiteralValue value = constEvaluator.evaluateRequired(initNode);
    if (type.isFloat()) return Constant.floatConst(value.asFloat());
    return Constant.intConst(value.asInt());
  }

  /**
   * Builds a nested constant array initializer.
   *
   * <p>This code relies on Sema to have normalized the brace structure enough that recursive
   * descent here matches the intended element layout.
   */
  private Constant buildGlobalArrayInit(Node initList, Type arrType) {
    if (isAllZeroInit(initList)) return Constant.zero(arrType);

    List<Constant> elems = new ArrayList<>();
    Type elemType = arrType.innerType;
    for (Node child : initList.kids) {
      if (elemType.isArray()) {
        if (child.tag == INIT_LIST) elems.add(buildGlobalArrayInit(child, elemType));
        else elems.add(Constant.zero(elemType));
      } else {
        elems.add(buildGlobalInit(child, elemType));
      }
    }
    return Constant.array(arrType, elems);
  }

  private boolean isAllZeroInit(Node initList) {
    for (Node child : initList.kids) {
      if (child.tag == INIT_LIST) { if (!isAllZeroInit(child)) return false; }
      else {
        var value = constEvaluator.evaluate(child);
        if (value.isEmpty() || !value.get().isZero()) return false;
      }
    }
    return true;
  }

  /**
   * Lowers one function from AST to IR.
   *
   * <p>The central choice here is to materialize every source variable, including parameters, as an
   * entry-block stack slot first. That gives the frontend a uniform "everything addressable" model:
   * assignments always store to a pointer, references usually load from a pointer, and later SSA
   * recovery is delegated to {@link Mem2Reg}.
   */
  private void emitFunction(Node func) {
    Type retType = Type.fromSysY(func.ty);
    curFunc = new Function(func.s, retType);
    module.addFunction(curFunc);
    funcRegistry.put(func.s, curFunc);

    enterScope();

    // Parameters
    int nParams = func.kids.size() - 1;
    List<Function.Argument> args = new ArrayList<>();
    for (int i = 0; i < nParams; i++) {
      Node parm = func.kids.get(i);
      Type pType = parm.ty.isArray() ? Type.PTR : Type.fromSysY(parm.ty);
      args.add(curFunc.addArgument(pType, "%p" + i));
    }

    // Entry block
    BasicBlock entry = addBlock("entry");
    entryBB = entry;
    switchTo(entry);

    // Parameters first exist as formal IR arguments. We immediately spill each one into an
    // entry-block slot so the rest of expression/statement lowering can treat parameters and local
    // variables the same way.
    for (int i = 0; i < nParams; i++) {
      Node parm = func.kids.get(i);
      Type pType = parm.ty.isArray() ? Type.PTR : Type.fromSysY(parm.ty);
      Instruction alloca = b.createAllocaInEntry(pType, entryBB);
      b.createStore(args.get(i), alloca);
      putVar(parm.s, alloca, pType);
    }

    // Function body
    Node body = func.kids.get(func.kids.size() - 1);
    emitBlock(body);

    // Implicit return
    if (!b.isTerminated()) {
      if (retType == Type.VOID) b.createRetVoid();
      else if (retType.isFloat()) b.createRet(Constant.floatConst(0.0f));
      else b.createRet(Constant.intConst(0));
    }

    exitScope();
  }

  /**
   * Emits a lexical block with its own symbol/type metadata scopes.
   *
   * <p>This is not a CFG split by itself. Plain `{ ... }` blocks only affect visibility; explicit
   * control-flow constructs such as {@code if} and {@code while} are what create new basic blocks.
   */
  private void emitBlock(Node block) {
    enterScope();
    for (Node stmt : block.kids) emitStmt(stmt);
    exitScope();
  }

  private void emitStmt(Node n) {
    if (n == null || b.isTerminated()) return;
    switch (n.tag) {
      case DECL_STMT:
        for (Node d : n.kids) emitVarDecl(d);
        break;
      case BLOCK: emitBlock(n); break;
      case IF:    emitIf(n); break;
      case WHILE: emitWhile(n); break;
      case RET:   emitReturn(n); break;
      case BREAK:
        if (!breakTargets.isEmpty()) b.createBr(breakTargets.peek());
        break;
      case CONT:
        if (!contTargets.isEmpty()) b.createBr(contTargets.peek());
        break;
      default: emitExpr(n); break;
    }
  }

  /**
   * Lowers one local declaration.
   *
   * <p>Scalars become one stack slot plus an optional initializing store. Arrays also become one
   * stack slot, but their initializer is emitted element-by-element because local initialization is
   * executable code rather than a static constant object.
   */
  private void emitVarDecl(Node v) {
    Type irType = Type.fromSysY(v.ty);

    if (v.ty.isArray()) {
      Instruction alloca = b.createAllocaInEntry(irType, entryBB);
      putVar(v.s, alloca, irType);
      putVarNode(v.s, v);
      if (!v.kids.isEmpty() && v.kids.get(0).tag == INIT_LIST) {
        emitLocalArrayInit(alloca, v.kids.get(0), irType, v.ty.dims);
      }
    } else {
      Instruction alloca = b.createAllocaInEntry(irType, entryBB);
      putVar(v.s, alloca, irType);
      if (!v.kids.isEmpty()) {
        Value val = emitExpr(v.kids.get(0));
        val = ensureType(val, irType);
        b.createStore(val, alloca);
      }
    }
  }

  /**
   * Emits executable initialization for a local array.
   *
   * <p>The algorithm is intentionally simple:
   *
   * <p>1. store a whole-array zero initializer first
   *
   * <p>2. flatten the normalized initializer list into scalar element order
   *
   * <p>3. store back only the explicitly provided non-zero elements
   *
   * <p>This is slightly redundant, but it keeps frontend logic straightforward and matches the
   * language rule that omitted elements default to zero.
   */
  private void emitLocalArrayInit(Value base, Node initList, Type arrType, int[] dims) {
    b.createStore(Constant.zero(arrType), base);

    List<Node> flatElems = new ArrayList<>();
    flattenInitList(initList, flatElems);

    Type scalarType = arrType.scalarType();
    int totalElems = 1;
    for (int d : dims) totalElems *= d;

    for (int i = 0; i < flatElems.size() && i < totalElems; i++) {
      Node elem = flatElems.get(i);
      if (elem.tag == LIT && elem.literal.isZero()) continue;
      Value val = emitExpr(elem);
      val = ensureType(val, scalarType);
      Instruction gep = b.createGEP(scalarType, base,
          new Value[] {Constant.int64Const(i)}, true);
      b.createStore(val, gep);
    }
  }

  /** Flattens nested initializer braces into left-to-right scalar element order. */
  private void flattenInitList(Node node, List<Node> out) {
    if (node.tag == INIT_LIST) {
      for (Node child : node.kids) flattenInitList(child, out);
    } else {
      out.add(node);
    }
  }

  /**
   * Lowers an if/else into explicit CFG blocks.
   *
   * <p>The shape is current block -> conditional branch -> then/else -> merge. We only synthesize
   * the jump to the merge block when the branch body did not already terminate on its own.
   */
  private void emitIf(Node n) {
    Value cond = emitCond(n.kids.get(0));
    boolean hasElse = n.kids.size() > 2;

    BasicBlock thenBB = addBlock(nextLabel("if.then"));
    BasicBlock elseBB = hasElse ? addBlock(nextLabel("if.else")) : null;
    BasicBlock endBB = addBlock(nextLabel("if.end"));

    b.createCondBr(cond, thenBB, hasElse ? elseBB : endBB);

    switchTo(thenBB);
    emitStmt(n.kids.get(1));
    brAndSwitch(endBB);

    if (hasElse) {
      switchTo(elseBB);
      emitStmt(n.kids.get(2));
      brAndSwitch(endBB);
    }

    switchTo(endBB);
  }

  /**
   * Lowers a while-loop into condition, body, and exit blocks.
   *
   * <p>{@code break} and {@code continue} are implemented by pushing synthetic targets while the
   * loop body is being emitted, which also naturally supports nesting.
   */
  private void emitWhile(Node n) {
    BasicBlock condBB = addBlock(nextLabel("while.cond"));
    BasicBlock bodyBB = addBlock(nextLabel("while.body"));
    BasicBlock endBB = addBlock(nextLabel("while.end"));

    brAndSwitch(condBB);

    Value cond = emitCond(n.kids.get(0));
    b.createCondBr(cond, bodyBB, endBB);

    breakTargets.push(endBB);
    contTargets.push(condBB);

    switchTo(bodyBB);
    emitStmt(n.kids.get(1));
    brAndSwitch(condBB);

    breakTargets.pop();
    contTargets.pop();

    switchTo(endBB);
  }

  private void emitReturn(Node n) {
    if (n.kids.isEmpty()) {
      b.createRetVoid();
    } else {
      Value val = emitExpr(n.kids.get(0));
      val = ensureType(val, curFunc.getReturnType());
      b.createRet(val);
    }
  }

  /** Dispatches one expression node to the appropriate lowering routine. */
  private Value emitExpr(Node n) {
    if (n == null) return Constant.intConst(0);
    switch (n.tag) {
      case LIT:   return emitLiteral(n);
      case REF:   return emitRef(n);
      case BIN:   return emitBinary(n);
      case UNARY: return emitUnary(n);
      case CALL:  return emitCall(n);
      case SUB:   return emitSubscript(n);
      case CAST:  return emitCast(n);
      default:    return Constant.intConst(0);
    }
  }

  private Value emitLiteral(Node n) {
    if (n.literal.isFloat()) return Constant.floatConst(n.literal.asFloat());
    return Constant.intConst(n.literal.asInt());
  }

  /**
   * Lowers a source-level reference in rvalue position.
   *
   * <p>Three cases matter:
   *
   * <p>- scalar locals/globals: load the scalar value
   *
   * <p>- array objects: decay to a pointer to the first element
   *
   * <p>- pointer-valued variables (for example lowered array parameters): load the pointer itself
   */
  private Value emitRef(Node n) {
    Value ptr = lookupVar(n.s);
    Type type = lookupType(n.s);
    if (ptr == null || type == null) return Constant.intConst(0);

    if (type.isArray()) {
      return b.createGEP(type, ptr, new Value[] {
          Constant.int64Const(0), Constant.int64Const(0)}, true);
    }
    if (type.isPointer()) {
      return b.createLoad(Type.PTR, ptr);
    }
    return b.createLoad(type, ptr);
  }

  /**
   * Lowers binary expressions.
   *
   * <p>Assignments and short-circuit logical operators are special and are handled separately.
   * Ordinary left-associative arithmetic/comparison trees are emitted by walking down the left
   * spine first and then rebuilding iteratively. That avoids deep recursive emission for long
   * expression chains.
   */
  private Value emitBinary(Node n) {
    if (n.op == Op.ASSIGN) return emitAssign(n);
    if (n.op.isLogical()) return emitLogical(n);

    Deque<Node> spine = new ArrayDeque<>();
    Node cur = n;
    while (cur.tag == Node.Tag.BIN && !cur.op.isLogical() && cur.op != Op.ASSIGN) {
      spine.push(cur);
      cur = cur.kids.get(0);
    }
    Value lhs = emitExpr(cur);
    while (!spine.isEmpty()) {
      Node bin = spine.pop();
      Value rhs = emitExpr(bin.kids.get(1));
      lhs = emitBinOp(bin, lhs, rhs);
    }
    return lhs;
  }

  /**
   * Emits one already-type-directed binary operation.
   *
   * <p>The node's semantic type determines the common operand type, so mixed int/float expressions
   * are normalized before choosing the final IR opcode.
   */
  private Value emitBinOp(Node n, Value lhs, Value rhs) {
    Type resultType = (n.ty != null) ? Type.fromSysY(n.ty) : Type.INT;
    lhs = ensureType(lhs, resultType);
    rhs = ensureType(rhs, resultType);
    boolean isFloat = resultType.isFloat();

    if (n.op.isRelational()) {
      Value cmpResult;
      if (isFloat) {
        String pred = floatPred(n.op);
        cmpResult = b.createFCmp(pred, lhs, rhs);
      } else {
        String pred = intPred(n.op);
        cmpResult = b.createICmp(pred, lhs, rhs);
      }
      return b.createZExt(cmpResult, Type.INT);
    }

    // Arithmetic
    if (isFloat) {
      switch (n.op) {
        case ADD: return b.createFAdd(lhs, rhs);
        case SUB: return b.createFSub(lhs, rhs);
        case MUL: return b.createFMul(lhs, rhs);
        case DIV: return b.createFDiv(lhs, rhs);
        default:  return b.createFAdd(lhs, rhs);
      }
    } else {
      switch (n.op) {
        case ADD: return b.createAdd(lhs, rhs);
        case SUB: return b.createSub(lhs, rhs);
        case MUL: return b.createMul(lhs, rhs);
        case DIV: return b.createSDiv(lhs, rhs);
        case MOD: return b.createSRem(lhs, rhs);
        default:  return b.createAdd(lhs, rhs);
      }
    }
  }

  /**
   * Lowers short-circuit {@code &&} and {@code ||}.
   *
   * <p>Instead of introducing PHI nodes directly, this frontend follows the same memory-based style
   * used elsewhere: allocate one temporary result slot in the entry block, write the short-circuit
   * default value, evaluate the RHS only on the path where it is semantically required, then load
   * the final integer result at the merge block.
   */
  private Value emitLogical(Node n) {
    Instruction allocaReg = b.createAllocaInEntry(Type.INT, entryBB);

    Value lhs = emitExpr(n.kids.get(0));
    Value lhsBool = toBool(lhs);

    if (n.op == Op.AND) {
      b.createStore(Constant.intConst(0), allocaReg);
      BasicBlock rhsBB = addBlock(nextLabel("and.rhs"));
      BasicBlock endBB = addBlock(nextLabel("and.end"));
      b.createCondBr(lhsBool, rhsBB, endBB);

      switchTo(rhsBB);
      Value rhs = emitExpr(n.kids.get(1));
      Value rhsBool = toBool(rhs);
      Value ext = b.createZExt(rhsBool, Type.INT);
      b.createStore(ext, allocaReg);
      brAndSwitch(endBB);

      switchTo(endBB);
    } else {
      // OR
      b.createStore(Constant.intConst(1), allocaReg);
      BasicBlock rhsBB = addBlock(nextLabel("or.rhs"));
      BasicBlock endBB = addBlock(nextLabel("or.end"));
      b.createCondBr(lhsBool, endBB, rhsBB);

      switchTo(rhsBB);
      Value rhs = emitExpr(n.kids.get(1));
      Value rhsBool = toBool(rhs);
      Value ext = b.createZExt(rhsBool, Type.INT);
      b.createStore(ext, allocaReg);
      brAndSwitch(endBB);

      switchTo(endBB);
    }

    return b.createLoad(Type.INT, allocaReg);
  }

  /**
   * Lowers assignment by evaluating the RHS first, then materializing the LHS address.
   *
   * <p>The stored RHS is also returned so assignment can participate in larger expressions.
   */
  private Value emitAssign(Node n) {
    Value rhs = emitExpr(n.kids.get(1));
    Node lhs = n.kids.get(0);

    if (lhs.tag == Node.Tag.REF) {
      Value ptr = lookupVar(lhs.s);
      Type type = lookupType(lhs.s);
      rhs = ensureType(rhs, type);
      b.createStore(rhs, ptr);
      return rhs;
    } else if (lhs.tag == Node.Tag.SUB) {
      Value gep = emitGEP(lhs);
      Type elemType = lhs.ty != null ? Type.fromSysY(lhs.ty) : Type.INT;
      rhs = ensureType(rhs, elemType);
      b.createStore(rhs, gep);
      return rhs;
    }
    return rhs;
  }

  private Value emitUnary(Node n) {
    Value operand = emitExpr(n.kids.get(0));
    switch (n.op) {
      case NEG:
        if (operand.getType().isFloat()) return b.createFNeg(operand);
        return b.createSub(Constant.intConst(0), operand);
      case NOT: {
        Value boolVal = toBool(operand);
        Value xored = b.createXor(boolVal, Constant.boolConst(true));
        return b.createZExt(xored, Type.INT);
      }
      case POS: return operand;
      default:  return operand;
    }
  }

  /**
   * Lowers a direct call.
   *
   * <p>Argument values are emitted left-to-right and then adjusted to the declared parameter types
   * when that information is available. Builtin timing helpers also get their SysY source names
   * rewritten to the runtime entry points expected by the rest of the toolchain.
   */
  private Value emitCall(Node n) {
    Node ref = n.kids.get(0);
    String funcName = ref.s;

    boolean isStartStop = funcName.equals("starttime") || funcName.equals("stoptime");
    if (funcName.equals("starttime")) funcName = "_sysy_starttime";
    if (funcName.equals("stoptime")) funcName = "_sysy_stoptime";

    Function callee = funcRegistry.get(funcName);
    if (callee == null) {
      // User-defined function not yet registered
      Type retType = Type.VOID;
      if (ref.decl != null && ref.decl.ty != null) retType = Type.fromSysY(ref.decl.ty);
      callee = new Function(funcName, retType);
      funcRegistry.put(funcName, callee);
    }

    Type retType = callee.getReturnType();

    // Determine expected parameter types
    Type[] expectedParamTypes = RUNTIME_PARAMS.get(funcName);
    int nDeclParams = 0;
    if (ref.decl != null && ref.decl.tag == Node.Tag.FUNC && !ref.decl.kids.isEmpty()) {
      nDeclParams = ref.decl.kids.size() - 1;
    }

    // Emit arguments
    List<Value> argVals = new ArrayList<>();
    for (int i = 1; i < n.kids.size(); i++) {
      int paramIdx = i - 1;
      Value argVal = emitExpr(n.kids.get(i));

      if (nDeclParams > 0 && paramIdx < nDeclParams) {
        Node paramDecl = ref.decl.kids.get(paramIdx);
        if (paramDecl.ty != null && !paramDecl.ty.isArray()) {
          Type expectedType = Type.fromSysY(paramDecl.ty);
          argVal = ensureType(argVal, expectedType);
        }
      } else if (expectedParamTypes != null && paramIdx < expectedParamTypes.length) {
        Type expectedType = expectedParamTypes[paramIdx];
        if (!expectedType.isPointer()) argVal = ensureType(argVal, expectedType);
      }

      argVals.add(argVal);
    }

    // starttime/stoptime
    if (isStartStop && argVals.isEmpty()) {
      argVals.add(Constant.intConst(0));
    }

    return b.createCall(callee, retType, argVals.toArray(new Value[0]));
  }

  /**
   * Lowers subscripting.
   *
   * <p>A fully indexed expression loads the element value. A partially indexed array expression
   * returns the computed address so it can decay/persist as a pointer.
   */
  private Value emitSubscript(Node n) {
    Value gep = emitGEP(n);
    Type elemType = n.ty != null ? Type.fromSysY(n.ty) : Type.INT;

    if (n.ty != null && n.ty.isArray()) {
      return gep; // partial subscript: return pointer
    }
    return b.createLoad(elemType, gep);
  }

  /**
   * Computes the address of a subscript expression.
   *
   * <p>This is subtle because the base expression may denote:
   *
   * <p>- an aggregate object like a local/global array
   *
   * <p>- a pointer variable such as a lowered array parameter
   *
   * <p>- an already-computed pointer-valued subexpression
   *
   * <p>Those cases require different GEP shapes, especially around whether a leading zero index is
   * needed.
   */
  private Value emitGEP(Node n) {
    Node baseNode = n.kids.get(0);

    if (baseNode.tag == Node.Tag.REF) {
      Value varPtr = lookupVar(baseNode.s);
      Type baseType = lookupType(baseNode.s);

      if (baseType != null && baseType.isPointer()) {
        Value loadedPtr = b.createLoad(Type.PTR, varPtr);
        Ty astTy = baseNode.decl != null ? baseNode.decl.ty : null;
        if (astTy != null && astTy.isArray() && astTy.dims.length > 0) {
          if (astTy.dims[0] == 0 && astTy.dims.length > 1) {
            Type elemType = Type.fromSysY(astTy.deref());
            return emitGEPIndices(loadedPtr, elemType, n.kids, 1, true);
          } else if (astTy.dims[0] == 0) {
            Type elemType = astTy.isFloat() ? Type.FLOAT : Type.INT;
            return emitGEPIndices(loadedPtr, elemType, n.kids, 1, true);
          }
        }
        Type scalarType = astTy != null && astTy.isFloat() ? Type.FLOAT : Type.INT;
        return emitGEPIndices(loadedPtr, scalarType, n.kids, 1, true);
      } else if (baseType != null && baseType.isArray()) {
        return emitGEPIndices(varPtr, baseType, n.kids, 1, false);
      } else {
        Type scalarType = baseType != null ? baseType : Type.INT;
        return emitGEPIndices(varPtr, scalarType, n.kids, 1, true);
      }
    } else {
      Value baseVal = emitExpr(baseNode);
      Ty astTy = baseNode.ty;
      if (astTy != null && astTy.isArray()) {
        Type t = Type.fromSysY(astTy);
        return emitGEPIndices(baseVal, t, n.kids, 1, true);
      }
      Type scalarType = astTy != null && astTy.isFloat() ? Type.FLOAT : Type.INT;
      return emitGEPIndices(baseVal, scalarType, n.kids, 1, true);
    }
  }

  /**
   * Applies one or more indices to a base pointer.
   *
   * <p>When {@code isFlat} is true, indexing proceeds one pointer step at a time, which matches
   * decayed pointers and array parameters. Otherwise we emit a single aggregate-style GEP with the
   * leading zero index needed to step into an array object stored in memory.
   */
  private Value emitGEPIndices(
      Value basePtr, Type baseType, List<Node> kids, int startIdx, boolean isFlat) {
    Value ptr = basePtr;

    if (isFlat) {
      Type curType = baseType;
      for (int i = startIdx; i < kids.size(); i++) {
        Value idx = emitExpr(kids.get(i));
        idx = ensureType(idx, Type.INT);
        Value idxExt = b.createSExt(idx, Type.I64);
        ptr = b.createGEP(curType, ptr, new Value[] {idxExt}, true);
        if (curType.isArray()) curType = curType.innerType;
      }
      return ptr;
    } else {
      // Aggregate objects are addressed as "pointer to whole array object", so LLVM-style GEP needs
      // a leading zero to stay within the same object before applying the source-language indices.
      Type curType = baseType;
      List<Value> extIndices = new ArrayList<>();
      for (int i = startIdx; i < kids.size(); i++) {
        Value idx = emitExpr(kids.get(i));
        idx = ensureType(idx, Type.INT);
        extIndices.add(b.createSExt(idx, Type.I64));
        if (curType.isArray()) curType = curType.innerType;
      }

      Value[] allIndices = new Value[1 + extIndices.size()];
      allIndices[0] = Constant.int64Const(0); // i64 0 prefix
      for (int i = 0; i < extIndices.size(); i++) allIndices[i + 1] = extIndices.get(i);
      return b.createGEP(baseType, ptr, allIndices, true);
    }
  }

  private Value emitCast(Node n) {
    Value val = emitExpr(n.kids.get(0));
    Type target = Type.fromSysY(n.ty);
    return castValue(val, target);
  }

  /**
   * Lowers an expression used in control-flow position.
   *
   * <p>The result is always {@code i1}. Relational operators can produce that directly; logical
   * operators first compute the language-level integer result and then get normalized back to a
   * branch condition; everything else follows the usual "compare against zero" rule.
   */
  private Value emitCond(Node n) {
    if (n.tag == Node.Tag.BIN && n.op.isRelational()) return emitCompareI1(n);
    if (n.tag == Node.Tag.BIN && n.op.isLogical()) {
      Value val = emitLogical(n);
      return toBool(val);
    }
    if (n.tag == Node.Tag.UNARY && n.op == Op.NOT) {
      Value operand = emitExpr(n.kids.get(0));
      Value boolVal = toBool(operand);
      return b.createXor(boolVal, Constant.boolConst(true));
    }
    return toBool(emitExpr(n));
  }

  private Value emitCompareI1(Node n) {
    Value lhs = emitExpr(n.kids.get(0));
    Value rhs = emitExpr(n.kids.get(1));

    Type cmpType = (n.ty != null && n.ty.isFloat()) ? Type.FLOAT : Type.INT;
    if (lhs.getType().isFloat() || rhs.getType().isFloat()) cmpType = Type.FLOAT;

    lhs = ensureType(lhs, cmpType);
    rhs = ensureType(rhs, cmpType);

    if (cmpType.isFloat()) return b.createFCmp(floatPred(n.op), lhs, rhs);
    return b.createICmp(intPred(n.op), lhs, rhs);
  }

  /** Normalizes a value into branch-friendly boolean form ({@code i1}). */
  private Value toBool(Value v) {
    if (v.getType() == Type.I1) return v;
    if (v.getType().isFloat())
      return b.createFCmp("une", v, Constant.floatConst(0.0f));
    return b.createICmp("ne", v, Constant.intConst(0));
  }

  private Value castValue(Value val, Type target) {
    if (val.getType() == target) return val;
    if (target.isFloat() && val.getType().isInt()) return b.createSIToFP(val, Type.FLOAT);
    if (target.isInt() && val.getType().isFloat()) return b.createFPToSI(val, Type.INT);
    return val;
  }

  private Value ensureType(Value val, Type expected) {
    if (val.getType() == expected) return val;
    if (val.getType().isPointer() && expected.isPointer()) return val;
    return castValue(val, expected);
  }

  private static String intPred(Op op) {
    switch (op) {
      case LT: return "slt"; case LE: return "sle";
      case GT: return "sgt"; case GE: return "sge";
      case EQ: return "eq";  case NE: return "ne";
      default: return "eq";
    }
  }

  private static String floatPred(Op op) {
    switch (op) {
      case LT: return "olt"; case LE: return "ole";
      case GT: return "ogt"; case GE: return "oge";
      case EQ: return "oeq"; case NE: return "one";
      default: return "oeq";
    }
  }

}
