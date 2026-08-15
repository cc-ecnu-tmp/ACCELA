package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.Set;

public final class InstSimplify {
  private InstSimplify() {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (!runOnFunction(function)) {
        return PreservedAnalyses.all();
      }
      return PreservedAnalyses.none();
    }
  }

  public static boolean runOnFunction(Function function) {
    Set<Instruction> worklist = new LinkedHashSet<>();
    for (BasicBlock bb : function.getBlocks()) {
      worklist.addAll(bb.getInstructions());
    }

    boolean changed = false;
    int fuel = 10000;

    while (!worklist.isEmpty() && fuel-- > 0) {
      Instruction inst = worklist.iterator().next();
      worklist.remove(inst);
      if (inst.getParent() == null) continue;

      Value result = trySimplify(inst);
      if (result != null) {
        for (var use : new ArrayList<>(inst.getUses())) {
          if (use.getUser() instanceof Instruction user) {
            worklist.add(user);
          }
        }
        inst.replaceAllUsesWith(result);
        inst.eraseFromParent();
        changed = true;
        continue;
      }

      if (tryDeadCode(inst)) {
        changed = true;
        continue;
      }

      if (tryConstantBranch(inst, worklist)) {
        changed = true;
      }
    }
    return changed;
  }

  private static Value trySimplify(Instruction inst) {
    Value v = tryConstantFold(inst);
    if (v != null) return v;
    return tryAlgebraicSimplify(inst);
  }

  private static Value tryConstantFold(Instruction inst) {
    if (inst.getType().isVector()) return null;
    switch (inst.getOpcode()) {
      case ADD: case SUB: case MUL: case SDIV: case SREM: case SHL: case AND: case XOR: {
        if (!(inst.getOperand(0) instanceof Constant.Int a)) return null;
        if (!(inst.getOperand(1) instanceof Constant.Int b)) return null;
        Long r = evalInt(inst.getOpcode(), a.value, b.value);
        if (r == null) return null;
        return Constant.intConst(r);
      }
      case FADD: case FSUB: case FMUL: case FDIV:
      case FNEG: case FCMP: case SITOFP: case FPTOSI:
        return null;
      case ICMP: {
        if (!(inst.getOperand(0) instanceof Constant.Int a)) return null;
        if (!(inst.getOperand(1) instanceof Constant.Int b)) return null;
        Boolean r = evalICmp(inst.getPredicate(), a.value, b.value);
        if (r == null) return null;
        return Constant.boolConst(r);
      }
      case ZEXT: case SEXT: {
        if (!(inst.getOperand(0) instanceof Constant.Int a)) return null;
        if (inst.getType() == Type.I64) return Constant.int64Const(a.value);
        return Constant.intConst(a.value);
      }
      default: return null;
    }
  }

  private static Value tryAlgebraicSimplify(Instruction inst) {
    switch (inst.getOpcode()) {
      case ADD: {
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        if (isZero(inst.getOperand(0))) return inst.getOperand(1);
        return null;
      }
      case SUB: {
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        if (inst.getOperand(0) == inst.getOperand(1)) return zeroOf(inst.getType());
        return null;
      }
      case MUL: {
        if (isOne(inst.getOperand(1))) return inst.getOperand(0);
        if (isOne(inst.getOperand(0))) return inst.getOperand(1);
        if (isZero(inst.getOperand(0)) || isZero(inst.getOperand(1)))
          return zeroOf(inst.getType());
        return null;
      }
      case SDIV: {
        if (isOne(inst.getOperand(1))) return inst.getOperand(0);
        return null;
      }
      case SHL: case ASHR: {
        if (isZero(inst.getOperand(1))) return inst.getOperand(0);
        if (isZero(inst.getOperand(0))) return zeroOf(inst.getType());
        return null;
      }
      case AND: {
        if (inst.getOperand(0) == inst.getOperand(1)) return inst.getOperand(0);
        if (isZero(inst.getOperand(0)) || isZero(inst.getOperand(1)))
          return zeroOf(inst.getType());
        return null;
      }
      case XOR: {
        if (inst.getOperand(0) == inst.getOperand(1)) return zeroOf(inst.getType());
        return null;
      }
      case ICMP:
        return simplifyBooleanCompare(inst);
      case SELECT:
        if (inst.getOperand(0) instanceof Constant.Int condition)
          return inst.getOperand(condition.value != 0 ? 1 : 2);
        if (inst.getOperand(1) == inst.getOperand(2)) return inst.getOperand(1);
        return null;
      default: return null;
    }
  }

  private static Value simplifyBooleanCompare(Instruction compare) {
    if (!compare.getPredicate().equals("ne")) return null;
    Value value = isIntZero(compare.getOperand(1))
        ? compare.getOperand(0)
        : isIntZero(compare.getOperand(0)) ? compare.getOperand(1) : null;
    if (!(value instanceof Instruction extension)
        || extension.getOpcode() != Opcode.ZEXT
        || extension.getOperand(0).getType() != Type.I1) return null;
    // zext preserves the only two i1 values, so zext(c) != 0 is exactly c.
    return extension.getOperand(0);
  }

  private static boolean tryDeadCode(Instruction inst) {
    if (inst.isTerminator()) return false;
    if (!inst.hasResult()) return false;
    if (inst.hasUses()) return false;
    if (hasSideEffect(inst)) return false;
    inst.eraseFromParent();
    return true;
  }

  private static boolean tryConstantBranch(Instruction inst, Set<Instruction> worklist) {
    if (inst.getOpcode() != Opcode.CONDBR) return false;
    if (!(inst.getOperand(0) instanceof Constant.Int ci)) return false;
    BasicBlock target = ci.value != 0
        ? (BasicBlock) inst.getOperand(1)
        : (BasicBlock) inst.getOperand(2);
    BasicBlock bb = inst.getParent();
    CFGUpdateUtils.rewriteCondBrToBr(bb, target);
    for (Instruction i : target.getInstructions()) {
      worklist.add(i);
    }
    return true;
  }

  private static boolean hasSideEffect(Instruction inst) {
    switch (inst.getOpcode()) {
      case STORE: case CALL: case RET: case BR: case CONDBR:
        return true;
      default:
        return false;
    }
  }

  private static boolean isIntZero(Value v) {
    return v instanceof Constant.Int ci && ci.value == 0;
  }

  private static boolean isIntOne(Value v) {
    return v instanceof Constant.Int ci && ci.value == 1;
  }

  private static boolean isZero(Value value) {
    if (value instanceof Constant.Zero) return true;
    if (value instanceof Constant.Int integer) return integer.value == 0;
    return value instanceof Constant.Vector vector
        && vector.elements.stream().allMatch(InstSimplify::isZero);
  }

  private static boolean isOne(Value value) {
    if (value instanceof Constant.Int integer) return integer.value == 1;
    return value instanceof Constant.Vector vector
        && vector.elements.stream().allMatch(InstSimplify::isOne);
  }

  private static Constant zeroOf(Type type) {
    return type.isVector() ? Constant.zero(type) : Constant.intConst(0);
  }

  private static Long evalInt(Opcode op, long a, long b) {
    switch (op) {
      case ADD: return a + b;
      case SUB: return a - b;
      case MUL: return a * b;
      case SDIV: return b == 0 ? null : a / b;
      case SREM: return b == 0 ? null : a % b;
      case SHL: return b >= 0 && b < Integer.SIZE ? (long) ((int) a << b) : null;
      case AND: return a & b;
      case XOR: return a ^ b;
      default: return null;
    }
  }

  private static Boolean evalICmp(String pred, long a, long b) {
    switch (pred) {
      case "eq": return a == b;
      case "ne": return a != b;
      case "slt": return a < b;
      case "sle": return a <= b;
      case "sgt": return a > b;
      case "sge": return a >= b;
      default: return null;
    }
  }

}
