package accela.preprocess;

import static accela.ast.Node.Op.*;
import static accela.ast.Node.Tag.*;

import accela.ast.Node;
import accela.ast.Node.Op;
import accela.ast.TensorShape;
import accela.ast.Ty;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Desugars Tensor nodes directly into the existing array/vector AST before semantic analysis.
 * Fixed Tensors become arrays of row vectors; no source emitter or second parser is involved.
 */
public final class TensorLowering {
  private static final String PREFIX = "__accela_tensor_";

  private final Node unit;
  private final String file;
  private final Map<String, Integer> constants = new LinkedHashMap<>();
  private final Map<String, TensorSymbol> globals = new LinkedHashMap<>();
  private final Map<String, FunctionInfo> functions = new LinkedHashMap<>();
  private final Set<String> usedNames = new LinkedHashSet<>();
  private int generated;

  private TensorLowering(Node unit, String file) {
    this.unit = unit;
    this.file = file == null ? "<input>" : file;
  }

  public static boolean isRequired(Node node) {
    if (node.tensor || node.tensorMatmul) return true;
    if (node.dimExprs != null && node.dimExprs.stream().anyMatch(TensorLowering::isRequired)) return true;
    return node.kids.stream().anyMatch(TensorLowering::isRequired);
  }

  /** Mutates and returns the supplied translation unit. */
  public static Node lower(Node unit, String file) {
    TensorLowering pass = new TensorLowering(unit, file);
    pass.collectNames(unit);
    pass.collectSignatures();
    pass.inferReturnShapes();
    pass.lowerUnit();
    return unit;
  }

  private enum Storage { VECTOR_ROWS, DYNAMIC_SCALARS }

  private record TensorSymbol(
      String name, TensorShape shape, boolean constant, Storage storage) {}

  private static final class FunctionInfo {
    final Node source;
    final Ty scalarReturn;
    final List<TensorShape> parameters;
    TensorShape returnShape;

    FunctionInfo(Node source, List<TensorShape> parameters) {
      this.source = source;
      this.scalarReturn = source.ty;
      this.parameters = parameters;
    }
  }

  private record ScalarValue(List<Node> prefix, Node expression, Ty type) {}
  private record TensorValue(List<Node> prefix, TensorSymbol storage) {}
  private record CallParts(List<Node> prefix, Node call) {}

  private static final class Scope {
    final Scope parent;
    final Map<String, TensorSymbol> symbols = new LinkedHashMap<>();
    final Map<String, Ty> scalars = new LinkedHashMap<>();

    Scope(Scope parent) { this.parent = parent; }
    void put(TensorSymbol symbol) { symbols.put(symbol.name, symbol); }
    void putScalar(String name, Ty type) { scalars.put(name, type); }
    TensorSymbol find(String name) {
      TensorSymbol symbol = symbols.get(name);
      return symbol != null ? symbol : parent == null ? null : parent.find(name);
    }
    TensorSymbol require(String name) {
      TensorSymbol symbol = find(name);
      if (symbol == null) throw new IllegalArgumentException("unknown Tensor " + name);
      return symbol;
    }
    Ty scalar(String name) {
      Ty type = scalars.get(name);
      return type != null ? type : parent == null ? null : parent.scalar(name);
    }
  }

  private void collectNames(Node node) {
    if (node.s != null) usedNames.add(node.s);
    if (node.dimExprs != null) node.dimExprs.forEach(this::collectNames);
    node.kids.forEach(this::collectNames);
  }

  private void collectSignatures() {
    for (Node item : unit.kids) {
      if (item.tag == VAR) {
        rememberConstant(item);
        if (item.tensor) {
          TensorShape shape = fixedShape(item);
          item.tensorShape = shape;
          globals.put(item.s, fixedSymbol(item.s, shape, item.flag));
        }
      } else if (item.tag == FUNC) {
        if (functions.containsKey(item.s)) fail("duplicate function " + item.s);
        List<TensorShape> parameters = new ArrayList<>();
        for (int i = 0; i + 1 < item.kids.size(); i++) {
          Node parameter = item.kids.get(i);
          TensorShape shape = parameter.tensor ? parameterShape(parameter) : null;
          parameter.tensorShape = shape;
          parameters.add(shape);
        }
        functions.put(item.s,
            new FunctionInfo(item, Collections.unmodifiableList(parameters)));
      }
    }
  }

  private void rememberConstant(Node declaration) {
    if (!declaration.tensor && declaration.flag
        && (declaration.dimExprs == null || declaration.dimExprs.isEmpty())
        && declaration.kids.size() == 1) {
      Integer value = evalConst(declaration.kids.getFirst());
      if (value != null) constants.put(declaration.s, value);
    }
  }

  private TensorShape parameterShape(Node parameter) {
    if (!parameter.flag)
      fail("Tensor parameter " + parameter.s + " must omit its first dimension");
    int trailing = parameter.dimExprs == null ? 0 : parameter.dimExprs.size();
    if (trailing > 1)
      fail("Tensor parameter " + parameter.s + " must have form [] or [][N]");
    if (trailing == 0) return new TensorShape(parameter.ty, TensorShape.DYNAMIC);
    Integer columns = evalConst(parameter.dimExprs.getFirst());
    if (columns == null || columns <= 0)
      fail("Tensor parameter " + parameter.s + " requires a positive constant column count");
    return new TensorShape(parameter.ty, TensorShape.DYNAMIC, columns);
  }

  private TensorShape fixedShape(Node declaration) {
    if (declaration.dimExprs == null
        || declaration.dimExprs.isEmpty() || declaration.dimExprs.size() > 2)
      fail("Tensor " + declaration.s + " must have shape [N] or [M][N]");
    int[] dimensions = new int[declaration.dimExprs.size()];
    for (int i = 0; i < dimensions.length; i++) {
      Integer value = evalConst(declaration.dimExprs.get(i));
      if (value == null || value <= 0)
        fail("Tensor " + declaration.s + " dimensions must be positive constants");
      dimensions[i] = value;
    }
    return new TensorShape(declaration.ty, dimensions);
  }

  private void inferReturnShapes() {
    Scope global = globalScope();
    for (FunctionInfo function : functions.values()) {
      if (!function.source.tensor) continue;
      Scope scope = functionScope(function, global);
      List<TensorShape> returns = new ArrayList<>();
      collectReturns(function.source.kids.getLast(), scope, returns);
      if (returns.isEmpty())
        fail("Tensor-returning function " + function.source.s + " has no value return");
      TensorShape expected = returns.getFirst();
      if (expected == null || expected.dynamic())
        fail("Tensor return shape of " + function.source.s + " must be fully static");
      if (expected.element != function.source.ty)
        fail("Tensor return element type of " + function.source.s + " does not match");
      for (TensorShape shape : returns) {
        if (!expected.equals(shape))
          fail("all Tensor return paths in " + function.source.s + " must have one shape");
      }
      function.returnShape = expected;
      function.source.tensorShape = expected;
    }
  }

  private Scope globalScope() {
    Scope scope = new Scope(null);
    globals.values().forEach(scope::put);
    for (Node item : unit.kids)
      if (item.tag == VAR && !item.tensor) scope.putScalar(item.s, item.ty);
    return scope;
  }

  private Scope functionScope(FunctionInfo function, Scope parent) {
    Scope scope = new Scope(parent);
    for (int i = 0; i < function.parameters.size(); i++) {
      TensorShape shape = function.parameters.get(i);
      Node parameter = function.source.kids.get(i);
      if (shape != null) scope.put(parameterSymbol(parameter.s, shape));
      else scope.putScalar(parameter.s, parameter.ty);
    }
    return scope;
  }

  private void collectReturns(Node statement, Scope incoming, List<TensorShape> returns) {
    if (statement.tag == BLOCK) {
      Scope scope = new Scope(incoming);
      for (Node child : statement.kids) {
        if (child.tag == DECL_STMT) {
          for (Node declaration : child.kids) {
            rememberConstant(declaration);
            if (declaration.tensor) {
              TensorShape shape = fixedShape(declaration);
              declaration.tensorShape = shape;
              scope.put(fixedSymbol(declaration.s, shape, declaration.flag));
            } else scope.putScalar(declaration.s, declaration.ty);
          }
        }
        collectReturns(child, scope, returns);
      }
    } else if (statement.tag == RET && !statement.kids.isEmpty()) {
      TensorShape shape = shapeOf(statement.kids.getFirst(), incoming);
      if (shape != null) returns.add(shape);
    } else if (statement.tag == IF) {
      collectReturns(statement.kids.get(1), new Scope(incoming), returns);
      if (statement.kids.size() > 2)
        collectReturns(statement.kids.get(2), new Scope(incoming), returns);
    } else if (statement.tag == WHILE) {
      collectReturns(statement.kids.get(1), new Scope(incoming), returns);
    }
  }

  private void lowerUnit() {
    Scope global = globalScope();
    for (Node item : unit.kids) {
      if (item.tag == VAR && item.tensor) lowerFixedDeclaration(item, global, true);
      else if (item.tag == FUNC) lowerFunction(item, global);
    }
    clearTensorMarkers(unit);
  }

  private void clearTensorMarkers(Node node) {
    node.tensor = false;
    node.tensorMatmul = false;
    if (node.dimExprs != null) node.dimExprs.forEach(this::clearTensorMarkers);
    node.kids.forEach(this::clearTensorMarkers);
  }

  private void lowerFunction(Node function, Scope globalsScope) {
    FunctionInfo info = functions.get(function.s);
    Scope scope = functionScope(info, globalsScope);
    List<Node> original = new ArrayList<>(function.kids);
    Node body = original.removeLast();
    List<Node> parameters = new ArrayList<>();
    if (info.returnShape != null) {
      String resultName = hidden(function.s, "result");
      Node result = arrayParameter(resultName,
          Ty.vector(info.returnShape.element, info.returnShape.last()));
      result.tensorShape = info.returnShape;
      parameters.add(result);
      scope.put(new TensorSymbol(
          resultName, info.returnShape, false, Storage.VECTOR_ROWS));
      function.ty = Ty.VOID;
    }
    for (int index = 0; index < original.size(); index++) {
      Node parameter = original.get(index);
      TensorShape shape = info.parameters.get(index);
      if (shape == null) {
        parameters.add(parameter);
      } else {
        parameter.ty = shape.rank() == 1
            ? shape.element : Ty.vector(shape.element, shape.last());
        parameter.flag = true;
        parameter.dimExprs = new ArrayList<>();
        parameter.tensor = false;
        parameters.add(parameter);
      }
    }
    function.kids.clear();
    function.kids.addAll(parameters);
    function.kids.add(lowerBlock(body, scope, info));
  }

  private Node arrayParameter(String name, Ty element) {
    Node parameter = new Node(PARM, name, element);
    parameter.flag = true;
    parameter.dimExprs = new ArrayList<>();
    return parameter;
  }

  private Node lowerBlock(Node block, Scope parent, FunctionInfo function) {
    Scope scope = new Scope(parent);
    Node result = new Node(BLOCK);
    for (Node statement : block.kids)
      result.kids.addAll(lowerStatement(statement, scope, function));
    return result;
  }

  private List<Node> lowerStatement(Node statement, Scope scope, FunctionInfo function) {
    if (statement == null) return List.of();
    if (statement.tag == BLOCK) return List.of(lowerBlock(statement, scope, function));
    if (statement.tag == DECL_STMT) return lowerDeclarationStatement(statement, scope);
    if (statement.tag == RET) return lowerReturn(statement, scope, function);
    if (statement.tag == IF) return lowerIf(statement, scope, function);
    if (statement.tag == WHILE) return lowerWhile(statement, scope, function);
    if (statement.tag == BREAK || statement.tag == CONT) return List.of(statement);
    if (statement.tag == BIN && statement.op == ASSIGN
        && shapeOf(statement.kids.get(0), scope) != null) {
      return lowerTensorAssignment(statement.kids.get(0), statement.kids.get(1), scope);
    }
    if (shapeOf(statement, scope) != null) {
      TensorValue value = lowerTensor(statement, scope, null);
      return value.prefix;
    }
    ScalarValue value = lowerScalar(statement, scope);
    List<Node> code = new ArrayList<>(value.prefix);
    code.add(value.expression);
    return code;
  }

  private List<Node> lowerDeclarationStatement(Node statement, Scope scope) {
    List<Node> code = new ArrayList<>();
    for (Node declaration : statement.kids) {
      rememberConstant(declaration);
      if (!declaration.tensor) {
        scope.putScalar(declaration.s, declaration.ty);
        if (declaration.kids.size() == 1 && declaration.kids.getFirst().tag != INIT_LIST) {
          ScalarValue value = lowerScalar(declaration.kids.getFirst(), scope);
          code.addAll(value.prefix);
          declaration.kids.set(0, value.expression);
          code.add(declarationStatement(declaration));
        } else {
          code.add(declarationStatement(declaration));
        }
        continue;
      }
      TensorShape shape = declaration.tensorShape != null
          ? declaration.tensorShape : fixedShape(declaration);
      declaration.tensorShape = shape;
      TensorSymbol symbol = fixedSymbol(declaration.s, shape, declaration.flag);
      scope.put(symbol);
      Node initializer = declaration.kids.isEmpty() ? null : declaration.kids.getFirst();
      lowerFixedStorage(declaration, shape);
      if (initializer == null || initializer.tag == INIT_LIST) {
        if (initializer != null)
          declaration.kids.set(0, tensorInitializer(initializer, shape));
        code.add(declarationStatement(declaration));
      } else {
        if (declaration.flag)
          fail("const Tensor " + declaration.s + " requires a brace initializer");
        declaration.kids.clear();
        declaration.kids.add(new Node(INIT_LIST));
        code.add(declarationStatement(declaration));
        TensorValue value = lowerTensor(initializer, scope, shape);
        code.addAll(value.prefix);
        code.addAll(copy(symbol, value.storage));
      }
    }
    return code;
  }

  private void lowerFixedDeclaration(Node declaration, Scope scope, boolean global) {
    TensorShape shape = declaration.tensorShape != null
        ? declaration.tensorShape : fixedShape(declaration);
    scope.put(fixedSymbol(declaration.s, shape, declaration.flag));
    Node initializer = declaration.kids.isEmpty() ? null : declaration.kids.getFirst();
    if (initializer != null && initializer.tag != INIT_LIST) {
      if (global) fail("global Tensor " + declaration.s + " requires a brace initializer");
      fail("Tensor expression initializer must appear in a block");
    }
    lowerFixedStorage(declaration, shape);
    if (initializer != null) declaration.kids.set(0, tensorInitializer(initializer, shape));
  }

  private void lowerFixedStorage(Node declaration, TensorShape shape) {
    declaration.ty = Ty.vector(shape.element, shape.last());
    declaration.dimExprs = new ArrayList<>(List.of(Node.intLiteral(shape.rows())));
    declaration.tensor = false;
  }

  private List<Node> lowerReturn(Node statement, Scope scope, FunctionInfo function) {
    if (function.returnShape == null) {
      if (statement.kids.isEmpty()) return List.of(statement);
      if (shapeOf(statement.kids.getFirst(), scope) != null)
        fail("scalar function " + function.source.s + " cannot return Tensor");
      ScalarValue value = lowerScalar(statement.kids.getFirst(), scope);
      Node result = new Node(RET);
      result.kids.add(value.expression);
      List<Node> code = new ArrayList<>(value.prefix);
      code.add(result);
      return code;
    }
    if (statement.kids.isEmpty())
      fail("Tensor return in " + function.source.s + " requires a value");
    TensorValue value = lowerTensor(statement.kids.getFirst(), scope, function.returnShape);
    TensorSymbol output = new TensorSymbol(
        hidden(function.source.s, "result"), function.returnShape,
        false, Storage.VECTOR_ROWS);
    List<Node> code = new ArrayList<>(value.prefix);
    code.addAll(copy(output, value.storage));
    code.add(new Node(RET));
    return code;
  }

  private List<Node> lowerIf(Node statement, Scope scope, FunctionInfo function) {
    if (shapeOf(statement.kids.get(0), scope) != null) fail("Tensor condition is not defined");
    ScalarValue condition = lowerScalar(statement.kids.get(0), scope);
    Node result = new Node(IF);
    result.kids.add(condition.expression);
    result.kids.add(controlled(statement.kids.get(1), new Scope(scope), function));
    if (statement.kids.size() > 2)
      result.kids.add(controlled(statement.kids.get(2), new Scope(scope), function));
    List<Node> code = new ArrayList<>(condition.prefix);
    code.add(result);
    return code;
  }

  private List<Node> lowerWhile(Node statement, Scope scope, FunctionInfo function) {
    if (shapeOf(statement.kids.get(0), scope) != null) fail("Tensor condition is not defined");
    ScalarValue condition = lowerScalar(statement.kids.get(0), scope);
    if (!condition.prefix.isEmpty())
      fail("Tensor argument adaptation in while conditions is not supported");
    Node result = new Node(WHILE);
    result.kids.add(condition.expression);
    result.kids.add(controlled(statement.kids.get(1), new Scope(scope), function));
    return List.of(result);
  }

  private Node controlled(Node statement, Scope scope, FunctionInfo function) {
    if (statement.tag == BLOCK) return lowerBlock(statement, scope, function);
    Node block = new Node(BLOCK);
    block.kids.addAll(lowerStatement(statement, scope, function));
    return block;
  }

  private List<Node> lowerTensorAssignment(Node left, Node right, Scope scope) {
    TensorSymbol target = requireTensorLValue(left, scope);
    if (target.constant) fail("cannot assign to const Tensor " + target.name);
    if (target.shape.dynamic())
      fail("whole assignment to Tensor parameter requires an explicit fixed-shape destination");
    TensorValue value = lowerTensor(right, scope, target.shape);
    List<Node> code = new ArrayList<>(value.prefix);
    code.addAll(copy(target, value.storage));
    return code;
  }

  private TensorValue lowerTensor(Node expression, Scope scope, TensorShape preferred) {
    TensorShape inferred = shapeOf(expression, scope);
    if (inferred == null) fail("expected Tensor expression");
    TensorShape shape = inferred;
    if (preferred != null && !inferred.compatibleDimensions(preferred)) {
      fail("Tensor expression " + inferred + " does not fit destination " + preferred);
    }
    if (shape.dynamic()) {
      if (preferred == null || preferred.dynamic())
        fail("dynamic Tensor expression requires a fixed-shape destination");
      shape = preferred;
    }
    if (expression.tag == REF) {
      TensorSymbol source = scope.require(expression.s);
      if (source.storage == Storage.VECTOR_ROWS)
        return new TensorValue(List.of(), source);
      TensorSymbol result = temporary(shape);
      List<Node> code = new ArrayList<>();
      code.add(declarationStatement(temporaryDeclaration(result)));
      code.addAll(copy(result, source));
      return new TensorValue(code, result);
    }
    if (expression.tag == CALL) return lowerTensorCall(expression, scope);
    if (expression.tag == UNARY) {
      if (expression.op == NOT) fail("Tensor logical negation is not defined");
      TensorValue operand = lowerTensor(expression.kids.getFirst(), scope, shape);
      TensorSymbol result = temporary(shape);
      String index = fresh("row");
      List<Node> code = new ArrayList<>(operand.prefix);
      code.add(declarationStatement(temporaryDeclaration(result)));
      code.add(loop(index, rowCount(result),
          assign(row(result, ref(index)),
              unary(expression.op, row(operand.storage, ref(index))))));
      return new TensorValue(code, result);
    }
    if (expression.tag != BIN || expression.op == ASSIGN)
      fail("unsupported Tensor expression");
    if (expression.tensorMatmul) return lowerMatmul(expression, scope, shape);
    if (expression.op.isLogical() || expression.op.isRelational())
      fail("Tensor comparison and logical operators are not defined");

    TensorShape leftShape = shapeOf(expression.kids.get(0), scope);
    TensorShape rightShape = shapeOf(expression.kids.get(1), scope);
    TensorValue leftTensor = leftShape == null ? null
        : lowerTensor(expression.kids.get(0), scope, leftShape.dynamic() ? shape : null);
    TensorValue rightTensor = rightShape == null ? null
        : lowerTensor(expression.kids.get(1), scope, rightShape.dynamic() ? shape : null);
    ScalarValue leftScalar = leftShape == null ? lowerScalar(expression.kids.get(0), scope) : null;
    ScalarValue rightScalar = rightShape == null ? lowerScalar(expression.kids.get(1), scope) : null;
    if (expression.op == MOD && (shape.element == Ty.FLOAT
        || leftScalar != null && leftScalar.type == Ty.FLOAT
        || rightScalar != null && rightScalar.type == Ty.FLOAT)) {
      fail("remainder is only defined for tensor int");
    }
    TensorSymbol result = temporary(shape);
    List<Node> code = new ArrayList<>();
    if (leftTensor != null) code.addAll(leftTensor.prefix); else code.addAll(leftScalar.prefix);
    if (rightTensor != null) code.addAll(rightTensor.prefix); else code.addAll(rightScalar.prefix);
    code.add(declarationStatement(temporaryDeclaration(result)));
    String index = fresh("row");
    Node lhs = leftTensor == null ? leftScalar.expression : row(leftTensor.storage, ref(index));
    Node rhs = rightTensor == null ? rightScalar.expression : row(rightTensor.storage, ref(index));
    code.add(loop(index, rowCount(result),
        assign(row(result, ref(index)), binary(expression.op, lhs, rhs))));
    return new TensorValue(code, result);
  }

  private TensorValue lowerMatmul(
      Node expression, Scope scope, TensorShape resultShape) {
    TensorShape a = shapeOf(expression.kids.get(0), scope);
    TensorShape b = shapeOf(expression.kids.get(1), scope);
    if (a.rank() != 2 || b.rank() != 2 || resultShape.rank() != 2)
      fail("@ is only defined for rank-two Tensors");
    if (resultShape.dynamic())
      fail("@ with a pointer-style Tensor requires a fixed-shape destination");
    if (a.element != b.element || resultShape.element != a.element
        || resultShape.dimensions[1] != b.dimensions[1]
        || b.dimensions[0] != TensorShape.DYNAMIC
            && a.dimensions[1] != b.dimensions[0]) {
      fail("@ requires same-type A[M][K] and B[K][N]");
    }
    TensorShape leftShape = new TensorShape(
        a.element, resultShape.dimensions[0], a.dimensions[1]);
    TensorShape rightShape = new TensorShape(
        b.element, a.dimensions[1], b.dimensions[1]);
    TensorValue left = lowerTensor(expression.kids.get(0), scope, leftShape);
    TensorValue right = lowerTensor(expression.kids.get(1), scope, rightShape);
    TensorSymbol result = temporary(resultShape);
    String i = fresh("mat_i"), k = fresh("mat_k");
    List<Node> code = new ArrayList<>(left.prefix);
    code.addAll(right.prefix);
    code.add(declarationStatement(temporaryDeclaration(result)));
    Node accumulate = assign(row(result, ref(i)),
        binary(ADD, row(result, ref(i)),
            binary(MUL, row(right.storage, ref(k)), element(left.storage, ref(i), ref(k)))));
    Node inner = loop(k, Node.intLiteral(a.dimensions[1]), accumulate);
    Node body = block(assign(row(result, ref(i)), zero(resultShape.element)), inner);
    code.add(loop(i, Node.intLiteral(resultShape.dimensions[0]), body));
    return new TensorValue(code, result);
  }

  private TensorValue lowerTensorCall(Node sourceCall, Scope scope) {
    String name = sourceCall.kids.getFirst().s;
    FunctionInfo function = functions.get(name);
    if (function == null || function.returnShape == null)
      fail("function does not return Tensor: " + name);
    TensorSymbol result = temporary(function.returnShape);
    CallParts call = lowerCall(sourceCall, function, scope, result);
    List<Node> code = new ArrayList<>(call.prefix);
    code.add(declarationStatement(temporaryDeclaration(result)));
    code.add(call.call);
    return new TensorValue(code, result);
  }

  private ScalarValue lowerScalar(Node expression, Scope scope) {
    if (shapeOf(expression, scope) != null) fail("Tensor value used where scalar is required");
    if (expression.tag == LIT)
      return new ScalarValue(List.of(), expression, expression.type());
    if (expression.tag == REF)
      return new ScalarValue(List.of(), expression,
          scope.scalar(expression.s) == Ty.FLOAT ? Ty.FLOAT : Ty.INT);
    if (expression.tag == Node.Tag.SUB) return lowerSubscript(expression, scope);
    if (expression.tag == CALL) {
      String name = expression.kids.getFirst().s;
      FunctionInfo function = functions.get(name);
      if (function != null && function.returnShape != null)
        fail("Tensor-returning call requires Tensor context");
      CallParts parts = lowerCall(expression, function, scope, null);
      Ty type = function == null ? Ty.INT : function.scalarReturn;
      return new ScalarValue(parts.prefix, parts.call, type);
    }
    if (expression.tag == UNARY) {
      ScalarValue operand = lowerScalar(expression.kids.getFirst(), scope);
      return new ScalarValue(
          operand.prefix, unary(expression.op, operand.expression), operand.type);
    }
    if (expression.tag != BIN)
      return new ScalarValue(List.of(), expression, Ty.INT);
    ScalarValue left = lowerScalar(expression.kids.get(0), scope);
    ScalarValue right = lowerScalar(expression.kids.get(1), scope);
    List<Node> prefix = new ArrayList<>(left.prefix);
    prefix.addAll(right.prefix);
    Ty type = left.type == Ty.FLOAT || right.type == Ty.FLOAT ? Ty.FLOAT : Ty.INT;
    return new ScalarValue(
        prefix, binary(expression.op, left.expression, right.expression), type);
  }

  private ScalarValue lowerSubscript(Node source, Scope scope) {
    List<Node> indices = new ArrayList<>();
    Node base = source;
    while (base.tag == Node.Tag.SUB) {
      if (base.kids.size() > 2) {
        indices.addAll(0, base.kids.subList(1, base.kids.size()));
      } else {
        indices.addFirst(base.kids.get(1));
      }
      base = base.kids.getFirst();
    }
    TensorSymbol symbol = base.tag == REF ? scope.find(base.s) : null;
    List<Node> prefix = new ArrayList<>();
    if (symbol == null && shapeOf(base, scope) != null) {
      TensorValue value = lowerTensor(base, scope, null);
      prefix.addAll(value.prefix);
      symbol = value.storage;
    }
    if (symbol == null) {
      ScalarValue loweredBase = lowerScalar(base, scope);
      prefix.addAll(loweredBase.prefix);
      Node result = loweredBase.expression;
      for (Node index : indices) {
        ScalarValue lowered = lowerScalar(index, scope);
        prefix.addAll(lowered.prefix);
        result = sub(result, lowered.expression);
      }
      return new ScalarValue(prefix, result, Ty.INT);
    }
    if (indices.size() != symbol.shape.rank())
      fail("partial Tensor indexing is not defined for " + symbol.shape);
    List<Node> loweredIndices = new ArrayList<>();
    for (Node index : indices) {
      ScalarValue lowered = lowerScalar(index, scope);
      prefix.addAll(lowered.prefix);
      loweredIndices.add(lowered.expression);
    }
    Node result;
    if (symbol.storage == Storage.DYNAMIC_SCALARS) {
      result = sub(ref(symbol.name), loweredIndices.getFirst());
    } else {
      Node row = Node.intLiteral(0);
      for (int i = 0; i + 1 < loweredIndices.size(); i++) {
        row = i == 0 ? loweredIndices.get(i)
            : binary(ADD,
                binary(MUL, row, Node.intLiteral(symbol.shape.dimensions[i])),
                loweredIndices.get(i));
      }
      result = sub(sub(ref(symbol.name), row), loweredIndices.getLast());
    }
    return new ScalarValue(prefix, result, symbol.shape.element);
  }

  private CallParts lowerCall(
      Node sourceCall, FunctionInfo function, Scope scope, TensorSymbol output) {
    List<Node> prefix = new ArrayList<>(), arguments = new ArrayList<>();
    if (output != null) arguments.add(ref(output.name));
    int argumentCount = sourceCall.kids.size() - 1;
    if (function != null && argumentCount != function.parameters.size())
      fail("argument count mismatch for " + sourceCall.kids.getFirst().s);
    for (int index = 0; index < argumentCount; index++) {
      Node argument = sourceCall.kids.get(index + 1);
      TensorShape expected = function == null ? null : function.parameters.get(index);
      TensorShape actual = shapeOf(argument, scope);
      if (expected == null) {
        if (actual != null) fail("Tensor passed to scalar parameter");
        ScalarValue value = lowerScalar(argument, scope);
        prefix.addAll(value.prefix);
        arguments.add(value.expression);
        continue;
      }
      if (actual == null) fail("scalar passed to Tensor parameter");
      validateArgumentShape(expected, actual);
      TensorValue value = argument.tag == REF
          ? new TensorValue(List.of(), scope.require(argument.s))
          : lowerTensor(argument, scope, actual.dynamic() ? null : actual);
      prefix.addAll(value.prefix);
      arguments.add(ref(value.storage.name));
    }
    Node call = new Node(CALL);
    call.kids.add(ref(sourceCall.kids.getFirst().s));
    call.kids.addAll(arguments);
    return new CallParts(prefix, call);
  }

  private void validateArgumentShape(TensorShape expected, TensorShape actual) {
    if (!expected.sameTrailingDimensions(actual))
      fail("Tensor argument " + actual + " does not match parameter " + expected);
  }

  private TensorShape shapeOf(Node expression, Scope scope) {
    if (expression == null) return null;
    if (expression.tag == REF) {
      TensorSymbol symbol = scope.find(expression.s);
      return symbol == null ? null : symbol.shape;
    }
    if (expression.tag == Node.Tag.SUB) {
      int count = 0;
      Node base = expression;
      while (base.tag == Node.Tag.SUB) {
        count += base.kids.size() - 1;
        base = base.kids.getFirst();
      }
      TensorShape shape = shapeOf(base, scope);
      if (shape == null) return null;
      if (count > shape.rank()) fail("too many Tensor indices for " + shape);
      if (count < shape.rank()) fail("partial Tensor indexing is not defined for " + shape);
      return null;
    }
    if (expression.tag == CALL) {
      FunctionInfo function = functions.get(expression.kids.getFirst().s);
      return function == null ? null : function.returnShape;
    }
    if (expression.tag == UNARY) return shapeOf(expression.kids.getFirst(), scope);
    if (expression.tag != BIN) return null;
    TensorShape left = shapeOf(expression.kids.get(0), scope);
    TensorShape right = shapeOf(expression.kids.get(1), scope);
    if (expression.op == ASSIGN) return left;
    if (expression.tensorMatmul) {
      if (left == null || right == null || left.rank() != 2 || right.rank() != 2
          || left.element != right.element
          || right.dimensions[0] != TensorShape.DYNAMIC
              && left.dimensions[1] != right.dimensions[0]) {
        fail("@ requires same-type A[M][K] and B[K][N]");
      }
      return new TensorShape(left.element, left.dimensions[0], right.dimensions[1]);
    }
    if (expression.op.isLogical() || expression.op.isRelational()) {
      if (left != null || right != null)
        fail("Tensor comparison and logical operators are not defined");
      return null;
    }
    if (left == null && right == null) return null;
    if (left != null && right != null && !left.compatibleDimensions(right))
      fail("Tensor dimensions must match");
    TensorShape basis = left != null && !left.dynamic() ? left
        : right != null && !right.dynamic() ? right : left != null ? left : right;
    Ty element = basis.element;
    if (left != null && right != null
        && (left.element == Ty.FLOAT || right.element == Ty.FLOAT)) {
      element = Ty.FLOAT;
    } else {
      Node scalar = left == null ? expression.kids.get(0)
          : right == null ? expression.kids.get(1) : null;
      if (scalar != null && scalarType(scalar, scope) == Ty.FLOAT) element = Ty.FLOAT;
    }
    TensorShape result = new TensorShape(element, basis.dimensions);
    expression.tensorShape = result;
    return result;
  }

  private Ty scalarType(Node expression, Scope scope) {
    if (expression == null) return Ty.INT;
    if (expression.type() != null && expression.type().isFloat()) return Ty.FLOAT;
    if (expression.tag == REF) return scope.scalar(expression.s) == Ty.FLOAT ? Ty.FLOAT : Ty.INT;
    if (expression.tag == CALL) {
      FunctionInfo function = functions.get(expression.kids.getFirst().s);
      return function != null && function.scalarReturn == Ty.FLOAT ? Ty.FLOAT : Ty.INT;
    }
    if (expression.tag == BIN || expression.tag == UNARY) {
      for (Node child : expression.kids)
        if (scalarType(child, scope) == Ty.FLOAT) return Ty.FLOAT;
    }
    return Ty.INT;
  }

  private TensorSymbol requireTensorLValue(Node expression, Scope scope) {
    if (expression.tag != REF) fail("whole Tensor assignment target must be a variable");
    return scope.require(expression.s);
  }

  private List<Node> copy(TensorSymbol target, TensorSymbol source) {
    if (target.shape.dynamic())
      fail("whole assignment to Tensor parameter requires an explicit fixed-shape destination");
    if (!target.shape.compatibleDimensions(source.shape)) {
      fail("incompatible Tensor assignment shapes " + source.shape + " and " + target.shape);
    }
    if (target.shape.rank() == 1 && source.storage == Storage.DYNAMIC_SCALARS) {
      String index = fresh("copy_lane");
      return List.of(loop(index, Node.intLiteral(target.shape.last()),
          assign(scalarAt(target, ref(index)), scalarAt(source, ref(index)))));
    }
    String index = fresh("copy_row");
    return List.of(loop(index, rowCount(target),
        assign(row(target, ref(index)), row(source, ref(index)))));
  }

  private Node tensorInitializer(Node initializer, TensorShape shape) {
    List<Node> flat = new ArrayList<>(Collections.nCopies(shape.elements(), null));
    int used = fillInitializer(initializer, shape.dimensions, 0, 0, flat);
    if (used > flat.size()) fail("too many initializer elements for " + shape);
    Node rows = new Node(INIT_LIST);
    for (int row = 0; row < shape.rows(); row++) {
      Node vector = new Node(INIT_LIST);
      for (int lane = 0; lane < shape.last(); lane++) {
        Node value = flat.get(row * shape.last() + lane);
        vector.kids.add(value != null ? value : zero(shape.element));
      }
      rows.kids.add(vector);
    }
    return rows;
  }

  private int fillInitializer(
      Node initializer, int[] dimensions, int level, int offset, List<Node> flat) {
    if (offset >= flat.size()) return offset + 1;
    if (initializer.tag != INIT_LIST) {
      flat.set(offset, initializer);
      return offset + 1;
    }
    int end = offset + product(dimensions, level);
    int cursor = offset;
    for (Node child : initializer.kids) {
      if (child.tag == INIT_LIST && level + 1 < dimensions.length) {
        int sub = product(dimensions, level + 1);
        int relative = cursor - offset;
        if (relative % sub != 0) cursor += sub - relative % sub;
        cursor = fillInitializer(child, dimensions, level + 1, cursor, flat);
        int consumed = cursor - offset;
        if (consumed % sub != 0) cursor += sub - consumed % sub;
      } else {
        cursor = fillInitializer(child, dimensions, level + 1, cursor, flat);
      }
      if (cursor > end) return cursor;
    }
    return cursor;
  }

  private static int product(int[] dimensions, int start) {
    int result = 1;
    for (int i = start; i < dimensions.length; i++)
      result = Math.multiplyExact(result, dimensions[i]);
    return result;
  }

  private TensorSymbol fixedSymbol(String name, TensorShape shape, boolean constant) {
    return new TensorSymbol(name, shape, constant, Storage.VECTOR_ROWS);
  }

  private TensorSymbol parameterSymbol(String name, TensorShape shape) {
    return new TensorSymbol(name, shape, false,
        shape.rank() == 1 ? Storage.DYNAMIC_SCALARS : Storage.VECTOR_ROWS);
  }

  private TensorSymbol temporary(TensorShape shape) {
    if (shape.dynamic()) fail("cannot materialize a dynamic Tensor temporary");
    return fixedSymbol(fresh("tmp"), shape, false);
  }

  private Node temporaryDeclaration(TensorSymbol symbol) {
    Node declaration = new Node(
        VAR, symbol.name, Ty.vector(symbol.shape.element, symbol.shape.last()));
    declaration.dimExprs = new ArrayList<>(List.of(Node.intLiteral(symbol.shape.rows())));
    declaration.kids.add(new Node(INIT_LIST));
    declaration.tensorShape = symbol.shape;
    return declaration;
  }

  private Node row(TensorSymbol symbol, Node index) {
    if (symbol.storage == Storage.DYNAMIC_SCALARS)
      throw new IllegalArgumentException("rank-one dynamic Tensor has no vector row");
    return sub(ref(symbol.name), index);
  }

  private Node element(TensorSymbol symbol, Node row, Node lane) {
    return symbol.storage == Storage.DYNAMIC_SCALARS
        ? sub(ref(symbol.name), lane) : sub(this.row(symbol, row), lane);
  }

  private Node scalarAt(TensorSymbol symbol, Node index) {
    return symbol.storage == Storage.DYNAMIC_SCALARS
        ? sub(ref(symbol.name), index) : element(symbol, Node.intLiteral(0), index);
  }

  private Node rowCount(TensorSymbol symbol) {
    if (symbol.shape.dynamic())
      fail("dynamic Tensor row count requires a fixed-shape destination");
    return Node.intLiteral(symbol.shape.rows());
  }

  private Node loop(String index, Node bound, Node... statements) {
    Node declaration = new Node(VAR, index, Ty.INT);
    declaration.dimExprs = new ArrayList<>();
    declaration.kids.add(Node.intLiteral(0));
    Node condition = binary(LT, ref(index), bound);
    Node increment = assign(ref(index), binary(ADD, ref(index), Node.intLiteral(1)));
    Node body = new Node(BLOCK);
    body.kids.addAll(Arrays.asList(statements));
    body.kids.add(increment);
    Node loop = new Node(WHILE);
    loop.kids.add(condition);
    loop.kids.add(body);
    return block(declarationStatement(declaration), loop);
  }

  private static Node declarationStatement(Node... declarations) {
    Node statement = new Node(DECL_STMT);
    statement.kids.addAll(Arrays.asList(declarations));
    return statement;
  }

  private static Node block(Node... statements) {
    Node block = new Node(BLOCK);
    block.kids.addAll(Arrays.asList(statements));
    return block;
  }

  private static Node ref(String name) { return new Node(REF, name); }

  private static Node sub(Node base, Node index) {
    Node result = new Node(Node.Tag.SUB);
    result.kids.add(base);
    result.kids.add(index);
    return result;
  }

  private static Node binary(Op operator, Node left, Node right) {
    Node result = new Node(BIN);
    result.op = operator;
    result.kids.add(left);
    result.kids.add(right);
    return result;
  }

  private static Node assign(Node left, Node right) { return binary(ASSIGN, left, right); }

  private static Node unary(Op operator, Node operand) {
    Node result = new Node(UNARY);
    result.op = operator;
    result.kids.add(operand);
    return result;
  }

  private static Node zero(Ty type) {
    return type == Ty.FLOAT ? Node.floatLiteral(0.0f) : Node.intLiteral(0);
  }

  private Integer evalConst(Node expression) {
    try {
      if (expression == null) return null;
      if (expression.tag == LIT && !expression.literal.isFloat())
        return expression.literal.asInt();
      if (expression.tag == REF) return constants.get(expression.s);
      if (expression.tag == UNARY) {
        Integer value = evalConst(expression.kids.getFirst());
        if (value == null) return null;
        return switch (expression.op) {
          case NEG -> -value;
          case NOT -> value == 0 ? 1 : 0;
          case POS -> value;
          default -> null;
        };
      }
      if (expression.tag == BIN) {
        Integer left = evalConst(expression.kids.get(0));
        Integer right = evalConst(expression.kids.get(1));
        if (left == null || right == null) return null;
        return switch (expression.op) {
          case ADD -> left + right;
          case SUB -> left - right;
          case MUL -> left * right;
          case DIV -> left / right;
          case MOD -> left % right;
          default -> null;
        };
      }
    } catch (ArithmeticException ignored) {
      return null;
    }
    return null;
  }

  private String hidden(String owner, String role) { return PREFIX + role + "_" + owner; }

  private String fresh(String role) {
    String candidate;
    do {
      candidate = PREFIX + role + "_" + generated++;
    } while (!usedNames.add(candidate));
    return candidate;
  }

  private void fail(String message) {
    throw new IllegalArgumentException(file + ": tensor lowering: " + message);
  }
}
