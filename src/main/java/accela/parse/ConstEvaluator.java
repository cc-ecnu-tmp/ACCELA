package accela.parse;

import accela.ast.LiteralValue;
import accela.ast.Node;
import accela.ast.Node.Op;
import accela.ast.Node.Tag;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Optional;
import java.util.Set;

/** Evaluates Sema-bound AST expressions. */
public final class ConstEvaluator {
  public Optional<LiteralValue> evaluate(Node expression) {
    Set<Node> activeDecls = Collections.newSetFromMap(new IdentityHashMap<>());
    return evaluate(expression, activeDecls);
  }

  public LiteralValue evaluateRequired(Node expression) {
    return evaluate(expression)
        .orElseThrow(
            () -> new IllegalArgumentException("Constant expression required: " + expression.tag));
  }

  private Optional<LiteralValue> evaluate(Node node, Set<Node> activeDecls) {
    if (node == null) return Optional.empty();
    switch (node.tag) {
      case LIT:
        return Optional.of(node.literal);
      case UNARY:
        return evaluate(node.kids.get(0), activeDecls).map(value -> evaluateUnary(node.op, value));
      case BIN:
        return evaluateBinary(node, activeDecls);
      case CAST:
        return evaluate(node.kids.get(0), activeDecls).map(value -> cast(node, value));
      case REF:
        return evaluateReference(node, activeDecls);
      case SUB:
        return evaluateSubscript(node, activeDecls);
      default:
        return Optional.empty();
    }
  }

  private Optional<LiteralValue> evaluateBinary(Node node, Set<Node> activeDecls) {
    if (node.op == Op.ASSIGN) return Optional.empty();
    Optional<LiteralValue> lhs = evaluate(node.kids.get(0), activeDecls);
    if (lhs.isEmpty()) return Optional.empty();
    if (node.op == Op.AND && lhs.get().isZero()) return Optional.of(LiteralValue.ofInt(0));
    if (node.op == Op.OR && !lhs.get().isZero()) return Optional.of(LiteralValue.ofInt(1));

    Optional<LiteralValue> rhs = evaluate(node.kids.get(1), activeDecls);
    if (rhs.isEmpty()) return Optional.empty();
    LiteralValue left = lhs.get(), right = rhs.get();
    if (node.op.isLogical()) {
      return Optional.of(LiteralValue.ofInt(right.isZero() ? 0 : 1));
    }
    if (node.op.isRelational()) {
      return Optional.of(LiteralValue.ofInt(compare(node.op, left, right) ? 1 : 0));
    }
    return Optional.of(arithmetic(node.op, left, right));
  }

  private LiteralValue evaluateUnary(Op op, LiteralValue value) {
    if (op == Op.NOT) return LiteralValue.ofInt(value.isZero() ? 1 : 0);
    if (op == Op.POS) return value;
    if (op == Op.NEG) {
      return value.isFloat()
          ? LiteralValue.ofFloat(-value.asFloat())
          : LiteralValue.ofInt(-value.asInt());
    }
    throw new IllegalArgumentException("Unsupported constant unary operator: " + op);
  }

  private LiteralValue arithmetic(Op op, LiteralValue left, LiteralValue right) {
    if (left.isFloat() || right.isFloat()) {
      float lhs = left.asFloat(), rhs = right.asFloat();
      return switch (op) {
        case ADD -> LiteralValue.ofFloat(lhs + rhs);
        case SUB -> LiteralValue.ofFloat(lhs - rhs);
        case MUL -> LiteralValue.ofFloat(lhs * rhs);
        case DIV -> LiteralValue.ofFloat(lhs / rhs);
        default -> throw new IllegalArgumentException("Unsupported float operator: " + op);
      };
    }
    int lhs = left.asInt(), rhs = right.asInt();
    return switch (op) {
      case ADD -> LiteralValue.ofInt(lhs + rhs);
      case SUB -> LiteralValue.ofInt(lhs - rhs);
      case MUL -> LiteralValue.ofInt(lhs * rhs);
      case DIV -> LiteralValue.ofInt(lhs / rhs);
      case MOD -> LiteralValue.ofInt(lhs % rhs);
      default -> throw new IllegalArgumentException("Unsupported integer operator: " + op);
    };
  }

  private boolean compare(Op op, LiteralValue left, LiteralValue right) {
    if (left.isFloat() || right.isFloat()) {
      float lhs = left.asFloat(), rhs = right.asFloat();
      return switch (op) {
        case EQ -> lhs == rhs;
        case NE -> lhs != rhs;
        case LT -> lhs < rhs;
        case LE -> lhs <= rhs;
        case GT -> lhs > rhs;
        case GE -> lhs >= rhs;
        default -> throw new IllegalArgumentException("Unsupported comparison: " + op);
      };
    }
    int lhs = left.asInt(), rhs = right.asInt();
    return switch (op) {
      case EQ -> lhs == rhs;
      case NE -> lhs != rhs;
      case LT -> lhs < rhs;
      case LE -> lhs <= rhs;
      case GT -> lhs > rhs;
      case GE -> lhs >= rhs;
      default -> throw new IllegalArgumentException("Unsupported comparison: " + op);
    };
  }

  private LiteralValue cast(Node cast, LiteralValue value) {
    if (cast.ty == null) throw new IllegalArgumentException("Untyped constant cast");
    return cast.ty.isFloat()
        ? LiteralValue.ofFloat(value.asFloat())
        : LiteralValue.ofInt(value.asInt());
  }

  private Optional<LiteralValue> evaluateReference(Node ref, Set<Node> activeDecls) {
    Node decl = ref.decl;
    if (decl == null || !decl.flag || decl.kids.isEmpty() || !activeDecls.add(decl)) {
      return Optional.empty();
    }
    try {
      return evaluate(decl.kids.get(0), activeDecls);
    } finally {
      activeDecls.remove(decl);
    }
  }

  private Optional<LiteralValue> evaluateSubscript(Node subscript, Set<Node> activeDecls) {
    Node ref = subscript.kids.get(0);
    if (ref.tag != Tag.REF || ref.decl == null || !ref.decl.flag || ref.decl.kids.isEmpty()) {
      return Optional.empty();
    }
    Node initializer = ref.decl.kids.get(0);
    if (initializer.tag == Tag.INIT_LIST && initializer.kids.isEmpty()) {
      if (ref.decl.ty == null
          || !ref.decl.ty.isArray()
          || subscript.kids.size() - 1 != ref.decl.ty.dims.length) return Optional.empty();
      for (int i = 1; i < subscript.kids.size(); i++) {
        Optional<LiteralValue> index = evaluate(subscript.kids.get(i), activeDecls);
        if (index.isEmpty() || index.get().asInt() < 0
            || index.get().asInt() >= ref.decl.ty.dims[i - 1]) return Optional.empty();
      }
      return Optional.of(
          ref.decl.ty.isFloat() ? LiteralValue.ofFloat(0.0f) : LiteralValue.ofInt(0));
    }
    for (int i = 1; i < subscript.kids.size(); i++) {
      Optional<LiteralValue> index = evaluate(subscript.kids.get(i), activeDecls);
      if (index.isEmpty() || initializer.tag != Tag.INIT_LIST) return Optional.empty();
      int value = index.get().asInt();
      if (value < 0 || value >= initializer.kids.size()) return Optional.empty();
      initializer = initializer.kids.get(value);
    }
    return evaluate(initializer, activeDecls);
  }
}
