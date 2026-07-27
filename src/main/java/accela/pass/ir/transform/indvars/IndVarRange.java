package accela.pass.ir.transform.indvars;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;

/** The range implied by entering a canonical {@code for (i = 0; i < n; ++i)} body. */
record IndVarRange(Instruction phi, Value upperBound, BasicBlock body) {
  static IndVarRange from(InductionVariableAnalysis.Induction induction) {
    if (!(induction.start() instanceof Constant.Int start)
        || start.value != 0
        || induction.step() != 1) return null;
    Instruction branch = induction.loop().header().getTerminator();
    if (branch == null
        || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP
        || !"slt".equals(compare.getPredicate())
        || compare.getOperand(0) != induction.phi()) return null;
    BasicBlock body = (BasicBlock) branch.getOperand(1);
    BasicBlock exit = (BasicBlock) branch.getOperand(2);
    return induction.loop().contains(body) && !induction.loop().contains(exit)
        ? new IndVarRange(induction.phi(), compare.getOperand(1), body)
        : null;
  }

  boolean provesTrue(
      Instruction compare, DominatorTreeAnalysis.Result dominators) {
    if (!dominators.dominates(body, compare.getParent())) return false;
    Integer offset;
    if ("slt".equals(compare.getPredicate())
        && compare.getOperand(1) == upperBound
        && (offset = affineOffset(compare.getOperand(0))) != null) {
      return offset <= 0;
    }
    if ("sge".equals(compare.getPredicate())
        && isZero(compare.getOperand(1))
        && (offset = affineOffset(compare.getOperand(0))) != null) {
      // i < INT_MAX implies i + 1 cannot overflow; larger offsets can.
      return offset >= 0 && offset <= 1;
    }
    return false;
  }

  boolean provesNonNegative(
      Value value, Instruction context, DominatorTreeAnalysis.Result dominators) {
    if (!dominators.dominates(body, context.getParent())
        || !(upperBound instanceof Constant.Int bound)
        || bound.value <= 0
        || bound.value > Integer.MAX_VALUE) return false;
    Affine affine = affine(value);
    if (affine == null || affine.scale < 0 || affine.offset < 0) return false;
    long maximum = affine.scale * (bound.value - 1) + affine.offset;
    return maximum <= Integer.MAX_VALUE;
  }

  private Integer affineOffset(Value value) {
    Affine affine = affine(value);
    return affine != null && affine.scale == 1 ? (int) affine.offset : null;
  }

  private Affine affine(Value value) {
    if (value == phi) return new Affine(1, 0);
    if (value instanceof Constant.Int constant) {
      return bounded(0, (int) constant.value);
    }
    if (!(value instanceof Instruction instruction)
        || instruction.getNumOperands() != 2) return null;
    Affine left = affine(instruction.getOperand(0));
    Affine right = affine(instruction.getOperand(1));
    if (left != null && right != null) {
      if (instruction.getOpcode() == Instruction.Opcode.ADD) {
        return bounded(left.scale + right.scale, left.offset + right.offset);
      }
      if (instruction.getOpcode() == Instruction.Opcode.SUB) {
        return bounded(left.scale - right.scale, left.offset - right.offset);
      }
    }
    if (instruction.getOpcode() == Instruction.Opcode.MUL) {
      if (instruction.getOperand(0) instanceof Constant.Int constant && right != null) {
        int factor = (int) constant.value;
        return bounded(factor * right.scale, factor * right.offset);
      }
      if (instruction.getOperand(1) instanceof Constant.Int constant && left != null) {
        int factor = (int) constant.value;
        return bounded(factor * left.scale, factor * left.offset);
      }
    }
    return null;
  }

  private static Affine bounded(long scale, long offset) {
    return scale >= Integer.MIN_VALUE && scale <= Integer.MAX_VALUE
            && offset >= Integer.MIN_VALUE && offset <= Integer.MAX_VALUE
        ? new Affine(scale, offset)
        : null;
  }

  private record Affine(long scale, long offset) {}

  private static boolean isZero(Value value) {
    return value instanceof Constant.Int constant && constant.value == 0;
  }
}
