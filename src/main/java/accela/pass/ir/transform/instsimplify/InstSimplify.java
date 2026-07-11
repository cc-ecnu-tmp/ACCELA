package accela.pass.ir.transform.instsimplify;

import accela.utils.ir.CFGUpdateUtils;
import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
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

      if (strengthReduceRemainderCompare(inst, worklist)) changed = true;

      Value result = trySimplify(inst);
      if (result != null) {
        for (int index = 0; index < inst.getNumOperands(); index++) {
          if (inst.getOperand(index) instanceof Instruction producer) worklist.add(producer);
        }
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

  private static boolean strengthReduceRemainderCompare(
      Instruction compare, Set<Instruction> worklist) {
    if (compare.getOpcode() != Opcode.ICMP
        || !("eq".equals(compare.getPredicate()) || "ne".equals(compare.getPredicate()))) {
      return false;
    }
    int remainderIndex;
    if (isIntZero(compare.getOperand(1))) remainderIndex = 0;
    else if (isIntZero(compare.getOperand(0))) remainderIndex = 1;
    else return false;
    if (!(compare.getOperand(remainderIndex) instanceof Instruction remainder)
        || remainder.getOpcode() != Opcode.SREM
        || !(remainder.getOperand(1) instanceof Constant.Int divisor)
        || divisor.value <= 0 || (divisor.value & (divisor.value - 1)) != 0) return false;
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(compare);
    Constant.Int mask = remainder.getType() == Type.I64
        ? Constant.int64Const(divisor.value - 1) : Constant.intConst(divisor.value - 1);
    compare.setOperand(remainderIndex, builder.createAnd(remainder.getOperand(0), mask));
    worklist.add(remainder);
    return true;
  }

  private static Value trySimplify(Instruction inst) {
    Value v = tryConstantFold(inst);
    if (v != null) return v;
    return tryAlgebraicSimplify(inst);
  }

  private static Value tryConstantFold(Instruction inst) {
    switch (inst.getOpcode()) {
      case ADD: case SUB: case MUL: case SDIV: case SREM: case XOR: case AND: {
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
        if (isIntZero(inst.getOperand(1))) return inst.getOperand(0);
        if (isIntZero(inst.getOperand(0))) return inst.getOperand(1);
        return null;
      }
      case SUB: {
        if (isIntZero(inst.getOperand(1))) return inst.getOperand(0);
        if (inst.getOperand(0) == inst.getOperand(1)) return Constant.intConst(0);
        return null;
      }
      case MUL: {
        if (isIntOne(inst.getOperand(1))) return inst.getOperand(0);
        if (isIntOne(inst.getOperand(0))) return inst.getOperand(1);
        if (isIntZero(inst.getOperand(0)) || isIntZero(inst.getOperand(1)))
          return Constant.intConst(0);
        return null;
      }
      case SDIV: {
        if (isIntOne(inst.getOperand(1))) return inst.getOperand(0);
        return null;
      }
      case XOR: {
        if (inst.getOperand(0) == inst.getOperand(1)) return Constant.intConst(0);
        return null;
      }
      case ICMP: {
        if (!"ne".equals(inst.getPredicate()) || !isIntZero(inst.getOperand(1))) return null;
        if (inst.getOperand(0) instanceof Instruction extension
            && extension.getOpcode() == Opcode.ZEXT
            && extension.getOperand(0).getType() == Type.I1) {
          return extension.getOperand(0);
        }
        return inst.getOperand(0).getType() == Type.I1 ? inst.getOperand(0) : null;
      }
      default: return null;
    }
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

  private static Long evalInt(Opcode op, long a, long b) {
    switch (op) {
      case ADD: return a + b;
      case SUB: return a - b;
      case MUL: return a * b;
      case SDIV: return b == 0 ? null : a / b;
      case SREM: return b == 0 ? null : a % b;
      case XOR: return a ^ b;
      case AND: return a & b;
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
