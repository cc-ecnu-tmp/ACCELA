package accela.pass.ir.analysis.scev;

import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.LoopAnalysis;
import java.math.BigInteger;
import java.util.List;

/** Immutable scalar-evolution expressions. */
public sealed interface SCEV
    permits SCEV.Constant,
        SCEV.Unknown,
        SCEV.Add,
        SCEV.Multiply,
        SCEV.SignedDivide,
        SCEV.ZeroExtend,
        SCEV.SignExtend,
        SCEV.PointerAdd,
        SCEV.AddRec {
  Type getType();

  record Constant(Type type, BigInteger value) implements SCEV {
    public Constant {
      if (type == null || value == null) throw new NullPointerException();
    }

    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return value.toString();
    }
  }

  record Unknown(Value value) implements SCEV {
    public Unknown {
      if (value == null) throw new NullPointerException();
    }

    @Override
    public Type getType() {
      return value.getType();
    }

    @Override
    public String toString() {
      return value.getName() == null ? "unknown@" + identity(value) : "%" + value.getName();
    }
  }

  record Add(Type type, List<SCEV> operands) implements SCEV {
    public Add {
      operands = List.copyOf(operands);
      if (operands.size() < 2) throw new IllegalArgumentException("add needs two operands");
    }

    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return "(" + String.join(" + ", operands.stream().map(Object::toString).toList()) + ")";
    }
  }

  record Multiply(Type type, List<SCEV> operands) implements SCEV {
    public Multiply {
      operands = List.copyOf(operands);
      if (operands.size() < 2) throw new IllegalArgumentException("multiply needs two operands");
    }

    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return "(" + String.join(" * ", operands.stream().map(Object::toString).toList()) + ")";
    }
  }

  record SignedDivide(Type type, SCEV dividend, SCEV divisor) implements SCEV {
    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return "(" + dividend + " /s " + divisor + ")";
    }
  }

  record ZeroExtend(Type type, SCEV operand) implements SCEV {
    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return "zext(" + operand + " to " + type + ")";
    }
  }

  record SignExtend(Type type, SCEV operand) implements SCEV {
    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return "sext(" + operand + " to " + type + ")";
    }
  }

  /** A pointer base plus a byte offset. */
  record PointerAdd(SCEV base, SCEV byteOffset) implements SCEV {
    @Override
    public Type getType() {
      return Type.PTR;
    }

    @Override
    public String toString() {
      return "ptradd(" + base + ", " + byteOffset + ")";
    }
  }

  /** An affine recurrence: start + iteration * step in one loop. */
  record AddRec(Type type, SCEV start, SCEV step, LoopAnalysis.Loop loop) implements SCEV {
    public AddRec {
      if (type == null || start == null || step == null || loop == null) {
        throw new NullPointerException();
      }
    }

    @Override
    public Type getType() {
      return type;
    }

    @Override
    public String toString() {
      return "{" + start + ",+," + step + "}<" + loop.header().getLabel() + ">";
    }
  }

  private static String identity(Object object) {
    return Integer.toHexString(System.identityHashCode(object));
  }
}
