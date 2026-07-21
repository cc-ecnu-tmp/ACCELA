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
import accela.pass.ir.dataflow.ForwardDataflowSolver;
import accela.pass.ir.dataflow.ForwardTransfer;
import accela.pass.ir.dataflow.Lattice;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

public final class SCCP {
  private SCCP() {}

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      if (!runOnFunction(function)) {
        return PreservedAnalyses.all();
      }
      return PreservedAnalyses.none();
    }
  }

  enum ValKind { BOT, CONST, TOP }

  static final class ConstVal {
    final ValKind kind;
    final long intVal;
    final float floatVal;
    final Type type;

    private ConstVal(ValKind kind, long intVal, float floatVal, Type type) {
      this.kind = kind;
      this.intVal = intVal;
      this.floatVal = floatVal;
      this.type = type;
    }

    static final ConstVal BOT = new ConstVal(ValKind.BOT, 0, 0, null);
    static final ConstVal TOP = new ConstVal(ValKind.TOP, 0, 0, null);

    static ConstVal ofInt(long v, Type t) {
      return new ConstVal(ValKind.CONST, v, 0, t);
    }

    static ConstVal ofFloat(float v) {
      return new ConstVal(ValKind.CONST, 0, v, Type.FLOAT);
    }

    boolean isConst() { return kind == ValKind.CONST; }

    static ConstVal join(ConstVal a, ConstVal b) {
      if (a.kind == ValKind.BOT) return b;
      if (b.kind == ValKind.BOT) return a;
      if (a.kind == ValKind.TOP || b.kind == ValKind.TOP) return TOP;
      if (a.type == b.type && a.intVal == b.intVal
          && Float.floatToRawIntBits(a.floatVal) == Float.floatToRawIntBits(b.floatVal)) {
        return a;
      }
      return TOP;
    }

    boolean equals(ConstVal other) {
      if (kind != other.kind) return false;
      if (kind != ValKind.CONST) return true;
      return type == other.type && intVal == other.intVal
          && Float.floatToRawIntBits(floatVal) == Float.floatToRawIntBits(other.floatVal);
    }
  }

  static final class SCCPFact {
    final IdentityHashMap<Value, ConstVal> map;

    SCCPFact() { this.map = new IdentityHashMap<>(); }

    SCCPFact(IdentityHashMap<Value, ConstVal> map) { this.map = map; }

    ConstVal get(Value v) {
      if (v instanceof Constant.Int ci) return ConstVal.ofInt(ci.value, v.getType());
      if (v instanceof Constant.Float cf) return ConstVal.ofFloat(cf.value);
      if (v instanceof Constant.Zero) {
        if (v.getType().isFloat()) return ConstVal.ofFloat(0);
        return ConstVal.ofInt(0, v.getType());
      }
      return map.getOrDefault(v, ConstVal.BOT);
    }

    SCCPFact with(Value v, ConstVal cv) {
      IdentityHashMap<Value, ConstVal> copy = new IdentityHashMap<>(map);
      copy.put(v, cv);
      return new SCCPFact(copy);
    }
  }

  static final Lattice<SCCPFact> LATTICE = new Lattice<>() {
    @Override public SCCPFact bot() { return new SCCPFact(); }

    @Override public SCCPFact join(SCCPFact a, SCCPFact b) {
      IdentityHashMap<Value, ConstVal> result = new IdentityHashMap<>(a.map);
      for (var entry : b.map.entrySet()) {
        result.merge(entry.getKey(), entry.getValue(), ConstVal::join);
      }
      return new SCCPFact(result);
    }

    @Override public boolean isEqual(SCCPFact a, SCCPFact b) {
      if (a.map.size() != b.map.size()) return false;
      for (var entry : a.map.entrySet()) {
        ConstVal bv = b.map.get(entry.getKey());
        if (bv == null || !entry.getValue().equals(bv)) return false;
      }
      return true;
    }
  };

  static final class Analysis {
    final ForwardDataflowSolver.Result<SCCPFact> result;
    final SCCPTransfer transfer;

    Analysis(ForwardDataflowSolver.Result<SCCPFact> result, SCCPTransfer transfer) {
      this.result = result;
      this.transfer = transfer;
    }
  }

  static final class SCCPTransfer implements ForwardTransfer<SCCPFact> {

    interface CallResolver {
      ConstVal resolve(Instruction call, SCCPFact fact);
    }

    private Set<ForwardDataflowSolver.Edge> executableEdges = new LinkedHashSet<>();
    private CallResolver callResolver = (call, fact) -> ConstVal.TOP;

    void setExecutableEdges(Set<ForwardDataflowSolver.Edge> edges) {
      this.executableEdges = edges;
    }

    void setCallResolver(CallResolver resolver) {
      this.callResolver = resolver;
    }

    @Override
    public SCCPFact transferInstruction(Instruction inst, SCCPFact in) {
      if (inst.getOpcode() == Opcode.PHI) {
        BasicBlock phiBlock = inst.getParent();
        ConstVal result = ConstVal.BOT;
        for (int i = 0; i < inst.getNumOperands(); i += 2) {
          Value val = inst.getOperand(i);
          BasicBlock pred = (BasicBlock) inst.getOperand(i + 1);
          if (!executableEdges.contains(new ForwardDataflowSolver.Edge(pred, phiBlock)))
            continue;
          result = ConstVal.join(result, resolveValue(in, val));
        }
        return in.with(inst, result);
      }

      ConstVal result = evaluate(inst, in);
      if (result != null) {
        return in.with(inst, result);
      }
      return in;
    }

    private ConstVal resolveValue(SCCPFact fact, Value v) {
      if (v instanceof Constant.Int ci) return ConstVal.ofInt(ci.value, v.getType());
      if (v instanceof Constant.Float cf) return ConstVal.ofFloat(cf.value);
      if (v instanceof Constant.Zero) {
        if (v.getType().isFloat()) return ConstVal.ofFloat(0);
        return ConstVal.ofInt(0, v.getType());
      }
      return fact.get(v);
    }

    @Override
    public Map<BasicBlock, SCCPFact> transferTerminator(Instruction term, SCCPFact in) {
      Map<BasicBlock, SCCPFact> result = new HashMap<>();
      if (term.getOpcode() == Opcode.CONDBR) {
        ConstVal cond = in.get(term.getOperand(0));
        BasicBlock trueTarget = (BasicBlock) term.getOperand(1);
        BasicBlock falseTarget = (BasicBlock) term.getOperand(2);

        if (cond.kind == ValKind.BOT) return result;
        if (cond.isConst()) {
          if (cond.intVal != 0) {
            result.put(trueTarget, refineFact(in, term.getOperand(0), true));
          } else {
            result.put(falseTarget, refineFact(in, term.getOperand(0), false));
          }
          return result;
        }

        result.put(trueTarget, refineFact(in, term.getOperand(0), true));
        result.put(falseTarget, refineFact(in, term.getOperand(0), false));
        return result;
      }

      if (term.getOpcode() == Opcode.BR) {
        result.put((BasicBlock) term.getOperand(0), in);
        return result;
      }

      return result;
    }

    private SCCPFact refineFact(SCCPFact in, Value cond, boolean trueBranch) {
      if (!(cond instanceof Instruction icmp)) return in;
      if (icmp.getOpcode() != Opcode.ICMP) return in;
      String pred = icmp.getPredicate();
      if (!"eq".equals(pred)) return in;

      Value lhs = icmp.getOperand(0);
      Value rhs = icmp.getOperand(1);

      if (trueBranch) {
        if (rhs instanceof Constant.Int ci) return in.with(lhs, ConstVal.ofInt(ci.value, lhs.getType()));
        if (lhs instanceof Constant.Int ci) return in.with(rhs, ConstVal.ofInt(ci.value, rhs.getType()));
      }
      return in;
    }

    private ConstVal evaluate(Instruction inst, SCCPFact in) {
      switch (inst.getOpcode()) {
        case ADD: return evalIntBinop(inst, in, Long::sum);
        case SUB: return evalIntBinop(inst, in, (a, b) -> a - b);
        case MUL: return evalIntBinop(inst, in, (a, b) -> a * b);
        case SDIV: return evalIntBinop(inst, in, (a, b) -> b == 0 ? null : a / b);
        case SREM: return evalIntBinop(inst, in, (a, b) -> b == 0 ? null : a % b);
        case XOR: return evalIntBinop(inst, in, (a, b) -> a ^ b);
        case FADD: case FSUB: case FMUL: case FDIV: case FNEG:
          return ConstVal.TOP;
        case ICMP: return evalICmp(inst, in);
        case FCMP: return ConstVal.TOP;
        case ZEXT: case SEXT: {
          ConstVal op = in.get(inst.getOperand(0));
          if (op.kind == ValKind.BOT) return ConstVal.BOT;
          if (op.isConst()) return ConstVal.ofInt(op.intVal, inst.getType());
          return ConstVal.TOP;
        }
        case SITOFP: {
          ConstVal op = in.get(inst.getOperand(0));
          if (op.kind == ValKind.BOT) return ConstVal.BOT;
          if (op.isConst()) return ConstVal.ofFloat((float) op.intVal);
          return ConstVal.TOP;
        }
        case FPTOSI: {
          ConstVal op = in.get(inst.getOperand(0));
          if (op.kind == ValKind.BOT) return ConstVal.BOT;
          if (op.isConst()) return ConstVal.ofInt((long)(int) op.floatVal, inst.getType());
          return ConstVal.TOP;
        }
        case CALL: return callResolver.resolve(inst, in);
        case LOAD: return ConstVal.TOP;
        case STORE: case ALLOCA: case GEP: case BR: case CONDBR: case RET:
          return null;
        default:
          return ConstVal.TOP;
      }
    }

    @FunctionalInterface
    interface LongBinOp { Long apply(long a, long b); }

    private ConstVal evalIntBinop(Instruction inst, SCCPFact in, LongBinOp op) {
      ConstVal lhs = in.get(inst.getOperand(0));
      ConstVal rhs = in.get(inst.getOperand(1));
      if (lhs.kind == ValKind.BOT || rhs.kind == ValKind.BOT) return ConstVal.BOT;
      if (lhs.isConst() && rhs.isConst()) {
        Long result = op.apply(lhs.intVal, rhs.intVal);
        if (result == null) return ConstVal.TOP;
        return ConstVal.ofInt(result, inst.getType());
      }
      return ConstVal.TOP;
    }

    private ConstVal evalICmp(Instruction inst, SCCPFact in) {
      ConstVal lhs = in.get(inst.getOperand(0));
      ConstVal rhs = in.get(inst.getOperand(1));
      if (lhs.kind == ValKind.BOT || rhs.kind == ValKind.BOT) return ConstVal.BOT;
      if (!lhs.isConst() || !rhs.isConst()) return ConstVal.TOP;
      String pred = inst.getPredicate();
      boolean result;
      switch (pred) {
        case "eq":  result = lhs.intVal == rhs.intVal; break;
        case "ne":  result = lhs.intVal != rhs.intVal; break;
        case "slt": result = lhs.intVal <  rhs.intVal; break;
        case "sle": result = lhs.intVal <= rhs.intVal; break;
        case "sgt": result = lhs.intVal >  rhs.intVal; break;
        case "sge": result = lhs.intVal >= rhs.intVal; break;
        default: return ConstVal.TOP;
      }
      return ConstVal.ofInt(result ? 1 : 0, Type.I1);
    }
  }

  public static boolean runOnFunction(Function function) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    SCCPFact entryFact = new SCCPFact();
    for (var arg : function.getArguments()) {
      entryFact = entryFact.with(arg, ConstVal.TOP);
    }

    Analysis analysis = analyze(function, entryFact, null);
    return applyTransformations(function, analysis.result, analysis.transfer);
  }

  static Analysis analyze(
      Function function, SCCPFact entryFact, SCCPTransfer.CallResolver resolver) {
    SCCPTransfer transfer = new SCCPTransfer();
    if (resolver != null) transfer.setCallResolver(resolver);
    Set<ForwardDataflowSolver.Edge> liveEdges = new LinkedHashSet<>();
    transfer.setExecutableEdges(liveEdges);
    ForwardDataflowSolver.Result<SCCPFact> result =
        new ForwardDataflowSolver<SCCPFact>()
            .solve(function, LATTICE, transfer, entryFact, liveEdges);
    return new Analysis(result, transfer);
  }

  static ConstVal returnedValue(Analysis analysis) {
    ConstVal returned = ConstVal.BOT;
    for (BasicBlock block : analysis.result.reachableBlocks) {
      SCCPFact fact = analysis.result.blockFacts.getOrDefault(block, LATTICE.bot());
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Opcode.RET) {
          if (instruction.getNumOperands() != 0) {
            returned = ConstVal.join(returned, fact.get(instruction.getOperand(0)));
          }
          break;
        }
        if (!instruction.isTerminator()) {
          fact = analysis.transfer.transferInstruction(instruction, fact);
        }
      }
    }
    return returned;
  }

  static boolean applyTransformations(
      Function function,
      ForwardDataflowSolver.Result<SCCPFact> solveResult,
      SCCPTransfer transfer) {

    Map<BasicBlock, SCCPFact> blockFacts = solveResult.blockFacts;
    Set<BasicBlock> reachable = solveResult.reachableBlocks;
    boolean changed = false;
    SCCPFact bot = LATTICE.bot();

    for (BasicBlock bb : new ArrayList<>(function.getBlocks())) {
      if (!reachable.contains(bb)) continue;

      SCCPFact running = blockFacts.getOrDefault(bb, bot);
      for (Instruction inst : new ArrayList<>(bb.getInstructions())) {
        if (inst.getOpcode() == Opcode.PHI) {
          running = transfer.transferInstruction(inst, running);
          ConstVal cv = running.get(inst);
          if (cv.isConst() && inst.hasResult()) {
            inst.replaceAllUsesWith(makeConstant(cv));
            inst.eraseFromParent();
            changed = true;
          }
          continue;
        }

        if (inst.isTerminator()) {
          if (inst.getOpcode() == Opcode.CONDBR) {
            ConstVal cond = running.get(inst.getOperand(0));
            if (cond.isConst()) {
              BasicBlock target = cond.intVal != 0
                  ? (BasicBlock) inst.getOperand(1)
                  : (BasicBlock) inst.getOperand(2);
              CFGUpdateUtils.rewriteCondBrToBr(bb, target);
              changed = true;
            }
          }
          continue;
        }

        for (int i = 0; i < inst.getNumOperands(); i++) {
          Value op = inst.getOperand(i);
          if (op instanceof BasicBlock || op instanceof Function) continue;
          ConstVal cv = running.get(op);
          if (cv.isConst() && !(op instanceof Constant)) {
            inst.setOperand(i, makeConstant(cv));
            changed = true;
          }
        }

        running = transfer.transferInstruction(inst, running);

        ConstVal cv = running.get(inst);
        if (cv.isConst() && inst.hasResult()) {
          inst.replaceAllUsesWith(makeConstant(cv));
          if (inst.getOpcode() != Opcode.CALL) inst.eraseFromParent();
          changed = true;
        }
      }
    }
    return changed;
  }

  private static Value makeConstant(ConstVal cv) {
    if (cv.type == Type.FLOAT) return Constant.floatConst(cv.floatVal);
    if (cv.type == Type.I1) return Constant.boolConst(cv.intVal != 0);
    if (cv.type == Type.I64) return Constant.int64Const(cv.intVal);
    return Constant.intConst(cv.intVal);
  }
}
