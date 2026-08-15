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
import java.util.Collections;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
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
    final Constant constant;

    private ConstVal(ValKind kind, Constant constant) {
      this.kind = kind;
      this.constant = constant;
    }

    static final ConstVal BOT = new ConstVal(ValKind.BOT, null);
    static final ConstVal TOP = new ConstVal(ValKind.TOP, null);

    static ConstVal ofConstant(Constant constant) {
      return new ConstVal(ValKind.CONST, constant);
    }

    boolean isConst() { return kind == ValKind.CONST; }

    static ConstVal join(ConstVal a, ConstVal b) {
      if (a.kind == ValKind.BOT) return b;
      if (b.kind == ValKind.BOT) return a;
      if (a.kind == ValKind.TOP || b.kind == ValKind.TOP) return TOP;
      if (sameConstant(a.constant, b.constant)) return a;
      return TOP;
    }

    boolean equals(ConstVal other) {
      if (kind != other.kind) return false;
      if (kind != ValKind.CONST) return true;
      return sameConstant(constant, other.constant);
    }
  }

  static final class SCCPFact {
    final IdentityHashMap<Value, ConstVal> map;

    SCCPFact() { this.map = new IdentityHashMap<>(); }

    SCCPFact(IdentityHashMap<Value, ConstVal> map) { this.map = map; }

    ConstVal get(Value v) {
      if (v instanceof Constant constant) return ConstVal.ofConstant(constant);
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
      if (v instanceof Constant constant) return ConstVal.ofConstant(constant);
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
          Long condition = integerValue(cond.constant);
          if (condition == null) {
            result.put(trueTarget, refineFact(in, term.getOperand(0), true));
            result.put(falseTarget, refineFact(in, term.getOperand(0), false));
          } else if (condition != 0) {
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
        if (rhs instanceof Constant constant) {
          return in.with(lhs, ConstVal.ofConstant(constant));
        }
        if (lhs instanceof Constant constant) {
          return in.with(rhs, ConstVal.ofConstant(constant));
        }
      }
      return in;
    }

    private ConstVal evaluate(Instruction inst, SCCPFact in) {
      switch (inst.getOpcode()) {
        case ADD: return evalIntBinop(inst, in, Long::sum);
        case SUB: return evalIntBinop(inst, in, (a, b) -> a - b);
        case MUL: return evalIntBinop(inst, in, (a, b) -> a * b);
        case SMULH: return evalIntBinop(
            inst, in, (a, b) -> ((long) (int) a * (long) (int) b) >> Integer.SIZE);
        case SDIV: return evalIntBinop(inst, in, (a, b) -> b == 0 ? null : a / b);
        case SREM: return evalIntBinop(inst, in, (a, b) -> b == 0 ? null : a % b);
        case SHL: return evalIntBinop(
            inst, in, (a, b) -> shiftLeft(scalarElement(inst.getType()), a, b));
        case ASHR: return evalIntBinop(
            inst, in, (a, b) -> arithmeticShiftRight(scalarElement(inst.getType()), a, b));
        case AND: return evalIntBinop(inst, in, (a, b) -> a & b);
        case XOR: return evalIntBinop(inst, in, (a, b) -> a ^ b);
        case FADD: return evalFloatBinop(inst, in, (a, b) -> a + b);
        case FSUB: return evalFloatBinop(inst, in, (a, b) -> a - b);
        case FMUL: return evalFloatBinop(inst, in, (a, b) -> a * b);
        case FDIV: return evalFloatBinop(inst, in, (a, b) -> a / b);
        case FNEG: return evalFNeg(inst, in);
        case ICMP: return evalICmp(inst, in);
        case FCMP: return evalFCmp(inst, in);
        case ZEXT: case SEXT: case SITOFP: case FPTOSI:
          return evalConversion(inst, in);
        case BUILD_VECTOR: return evalBuildVector(inst, in);
        case SPLAT: return evalSplat(inst, in);
        case EXTRACT_ELEMENT: return evalExtractElement(inst, in);
        case INSERT_ELEMENT: return evalInsertElement(inst, in);
        case SHUFFLE_VECTOR: return evalShuffleVector(inst, in);
        case SELECT: return evalSelect(inst, in);
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
        List<Constant> left = constantElements(lhs.constant);
        List<Constant> right = constantElements(rhs.constant);
        if (left == null || right == null || left.size() != right.size()) return ConstVal.TOP;
        Type elementType = scalarElement(inst.getType());
        List<Constant> result = new ArrayList<>();
        for (int lane = 0; lane < left.size(); lane++) {
          Long a = integerValue(left.get(lane));
          Long b = integerValue(right.get(lane));
          if (a == null || b == null) return ConstVal.TOP;
          Long folded = op.apply(a, b);
          if (folded == null) return ConstVal.TOP;
          result.add(integerConstant(elementType, folded));
        }
        return ConstVal.ofConstant(packConstant(inst.getType(), result));
      }
      return ConstVal.TOP;
    }

    private ConstVal evalICmp(Instruction inst, SCCPFact in) {
      ConstVal lhs = in.get(inst.getOperand(0));
      ConstVal rhs = in.get(inst.getOperand(1));
      if (lhs.kind == ValKind.BOT || rhs.kind == ValKind.BOT) return ConstVal.BOT;
      if (!lhs.isConst() || !rhs.isConst()) return ConstVal.TOP;
      List<Constant> left = constantElements(lhs.constant);
      List<Constant> right = constantElements(rhs.constant);
      if (left == null || right == null || left.size() != right.size()) return ConstVal.TOP;
      List<Constant> result = new ArrayList<>();
      for (int lane = 0; lane < left.size(); lane++) {
        Long a = integerValue(left.get(lane));
        Long b = integerValue(right.get(lane));
        if (a == null || b == null) return ConstVal.TOP;
        Boolean folded = compareInteger(inst.getPredicate(), a, b);
        if (folded == null) return ConstVal.TOP;
        result.add(Constant.boolConst(folded));
      }
      return ConstVal.ofConstant(packConstant(inst.getType(), result));
    }

    private ConstVal evalFloatBinop(Instruction inst, SCCPFact in, FloatBinOp op) {
      ConstVal lhs = in.get(inst.getOperand(0));
      ConstVal rhs = in.get(inst.getOperand(1));
      if (lhs.kind == ValKind.BOT || rhs.kind == ValKind.BOT) return ConstVal.BOT;
      if (!lhs.isConst() || !rhs.isConst()) return ConstVal.TOP;
      List<Constant> left = constantElements(lhs.constant);
      List<Constant> right = constantElements(rhs.constant);
      if (left == null || right == null || left.size() != right.size()) return ConstVal.TOP;
      List<Constant> result = new ArrayList<>();
      for (int lane = 0; lane < left.size(); lane++) {
        Float a = floatingValue(left.get(lane));
        Float b = floatingValue(right.get(lane));
        if (a == null || b == null) return ConstVal.TOP;
        result.add(Constant.floatConst(op.apply(a, b)));
      }
      return ConstVal.ofConstant(packConstant(inst.getType(), result));
    }

    private ConstVal evalFNeg(Instruction inst, SCCPFact in) {
      ConstVal operand = in.get(inst.getOperand(0));
      if (operand.kind == ValKind.BOT) return ConstVal.BOT;
      if (!operand.isConst()) return ConstVal.TOP;
      List<Constant> elements = constantElements(operand.constant);
      if (elements == null) return ConstVal.TOP;
      List<Constant> result = new ArrayList<>();
      for (Constant element : elements) {
        Float value = floatingValue(element);
        if (value == null) return ConstVal.TOP;
        result.add(Constant.floatConst(-value));
      }
      return ConstVal.ofConstant(packConstant(inst.getType(), result));
    }

    private ConstVal evalFCmp(Instruction inst, SCCPFact in) {
      ConstVal lhs = in.get(inst.getOperand(0));
      ConstVal rhs = in.get(inst.getOperand(1));
      if (lhs.kind == ValKind.BOT || rhs.kind == ValKind.BOT) return ConstVal.BOT;
      if (!lhs.isConst() || !rhs.isConst()) return ConstVal.TOP;
      List<Constant> left = constantElements(lhs.constant);
      List<Constant> right = constantElements(rhs.constant);
      if (left == null || right == null || left.size() != right.size()) return ConstVal.TOP;
      List<Constant> result = new ArrayList<>();
      for (int lane = 0; lane < left.size(); lane++) {
        Float a = floatingValue(left.get(lane));
        Float b = floatingValue(right.get(lane));
        if (a == null || b == null) return ConstVal.TOP;
        Boolean folded = compareFloat(inst.getPredicate(), a, b);
        if (folded == null) return ConstVal.TOP;
        result.add(Constant.boolConst(folded));
      }
      return ConstVal.ofConstant(packConstant(inst.getType(), result));
    }

    private ConstVal evalConversion(Instruction inst, SCCPFact in) {
      ConstVal operand = in.get(inst.getOperand(0));
      if (operand.kind == ValKind.BOT) return ConstVal.BOT;
      if (!operand.isConst()) return ConstVal.TOP;
      List<Constant> elements = constantElements(operand.constant);
      if (elements == null) return ConstVal.TOP;
      Type sourceType = scalarElement(inst.getOperand(0).getType());
      Type destinationType = scalarElement(inst.getType());
      List<Constant> result = new ArrayList<>();
      for (Constant element : elements) {
        Constant converted = convertConstant(
            inst.getOpcode(), element, sourceType, destinationType);
        if (converted == null) return ConstVal.TOP;
        result.add(converted);
      }
      return ConstVal.ofConstant(packConstant(inst.getType(), result));
    }

    private ConstVal evalBuildVector(Instruction inst, SCCPFact in) {
      List<Constant> elements = new ArrayList<>();
      for (int operand = 0; operand < inst.getNumOperands(); operand++) {
        ConstVal value = in.get(inst.getOperand(operand));
        if (value.kind == ValKind.BOT) return ConstVal.BOT;
        if (!value.isConst() || value.constant.getType().isVector()) return ConstVal.TOP;
        elements.add(value.constant);
      }
      return ConstVal.ofConstant(Constant.vector(inst.getType(), elements));
    }

    private ConstVal evalSplat(Instruction inst, SCCPFact in) {
      ConstVal value = in.get(inst.getOperand(0));
      if (value.kind == ValKind.BOT) return ConstVal.BOT;
      if (!value.isConst() || value.constant.getType().isVector()) return ConstVal.TOP;
      return ConstVal.ofConstant(Constant.vector(
          inst.getType(), Collections.nCopies(inst.getType().getLaneCount(), value.constant)));
    }

    private ConstVal evalExtractElement(Instruction inst, SCCPFact in) {
      ConstVal vector = in.get(inst.getOperand(0));
      ConstVal index = in.get(inst.getOperand(1));
      if (vector.kind == ValKind.BOT || index.kind == ValKind.BOT) return ConstVal.BOT;
      if (!vector.isConst() || !index.isConst()) return ConstVal.TOP;
      List<Constant> elements = constantElements(vector.constant);
      Long selected = integerValue(index.constant);
      if (elements == null || selected == null || selected < 0 || selected >= elements.size()) {
        return ConstVal.TOP;
      }
      return ConstVal.ofConstant(elements.get(selected.intValue()));
    }

    private ConstVal evalInsertElement(Instruction inst, SCCPFact in) {
      ConstVal vector = in.get(inst.getOperand(0));
      ConstVal element = in.get(inst.getOperand(1));
      ConstVal index = in.get(inst.getOperand(2));
      if (vector.kind == ValKind.BOT || element.kind == ValKind.BOT || index.kind == ValKind.BOT) {
        return ConstVal.BOT;
      }
      if (!vector.isConst() || !element.isConst() || !index.isConst()) return ConstVal.TOP;
      List<Constant> elements = constantElements(vector.constant);
      Long selected = integerValue(index.constant);
      if (elements == null || selected == null || selected < 0 || selected >= elements.size()) {
        return ConstVal.TOP;
      }
      List<Constant> result = new ArrayList<>(elements);
      result.set(selected.intValue(), element.constant);
      return ConstVal.ofConstant(Constant.vector(inst.getType(), result));
    }

    private ConstVal evalShuffleVector(Instruction inst, SCCPFact in) {
      ConstVal lhs = in.get(inst.getOperand(0));
      ConstVal rhs = in.get(inst.getOperand(1));
      if (lhs.kind == ValKind.BOT || rhs.kind == ValKind.BOT) return ConstVal.BOT;
      if (!lhs.isConst() || !rhs.isConst()
          || !(inst.getOperand(2) instanceof Constant.Vector mask)) return ConstVal.TOP;
      List<Constant> left = constantElements(lhs.constant);
      List<Constant> right = constantElements(rhs.constant);
      if (left == null || right == null) return ConstVal.TOP;
      List<Constant> choices = new ArrayList<>(left);
      choices.addAll(right);
      List<Constant> result = new ArrayList<>();
      for (Constant maskElement : mask.elements) {
        Long selected = integerValue(maskElement);
        if (selected == null || selected < 0 || selected >= choices.size()) return ConstVal.TOP;
        result.add(choices.get(selected.intValue()));
      }
      return ConstVal.ofConstant(Constant.vector(inst.getType(), result));
    }

    private ConstVal evalSelect(Instruction inst, SCCPFact in) {
      ConstVal condition = in.get(inst.getOperand(0));
      if (condition.kind == ValKind.BOT) return ConstVal.BOT;
      if (condition.isConst()) {
        Long value = integerValue(condition.constant);
        return value == null ? ConstVal.TOP : in.get(inst.getOperand(value != 0 ? 1 : 2));
      }
      ConstVal ifTrue = in.get(inst.getOperand(1));
      ConstVal ifFalse = in.get(inst.getOperand(2));
      if (ifTrue.isConst() && ifFalse.isConst() && ifTrue.constant == ifFalse.constant) return ifTrue;
      return ConstVal.TOP;
    }

    @FunctionalInterface
    interface FloatBinOp { float apply(float a, float b); }
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
              Long condition = integerValue(cond.constant);
              if (condition == null) continue;
              BasicBlock target = condition != 0
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
    if (!cv.isConst() || cv.constant == null) {
      throw new IllegalArgumentException("lattice value is not a constant");
    }
    return cv.constant;
  }

  private static Constant packConstant(Type type, List<Constant> elements) {
    if (type.isVector()) return Constant.vector(type, elements);
    if (elements.size() != 1 || !elements.getFirst().getType().equals(type)) {
      throw new IllegalArgumentException("constant result shape mismatch for " + type);
    }
    return elements.getFirst();
  }

  private static List<Constant> constantElements(Constant constant) {
    Type type = constant.getType();
    if (!type.isVector()) return List.of(constant);
    if (constant instanceof Constant.Vector vector) return vector.elements;
    if (constant instanceof Constant.Zero) {
      return Collections.nCopies(type.getLaneCount(), scalarZero(type.getElementType()));
    }
    return null;
  }

  private static Type scalarElement(Type type) {
    return type.isVector() ? type.getElementType() : type;
  }

  private static Constant integerConstant(Type type, long value) {
    if (type == Type.I1) return Constant.boolConst(value != 0);
    if (type == Type.I64) return Constant.int64Const(value);
    if (type == Type.INT) return Constant.intConst((int) value);
    throw new IllegalArgumentException("not an integer scalar type: " + type);
  }

  private static Constant scalarZero(Type type) {
    if (type == Type.FLOAT) return Constant.floatConst(0.0f);
    return integerConstant(type, 0);
  }

  private static Long integerValue(Constant constant) {
    if (constant instanceof Constant.Int integer) {
      if (constant.getType() == Type.I1) return integer.value == 0 ? 0L : 1L;
      if (constant.getType() == Type.INT) return (long) (int) integer.value;
      return integer.value;
    }
    if (constant instanceof Constant.Zero && constant.getType().isInteger()) return 0L;
    return null;
  }

  private static Float floatingValue(Constant constant) {
    if (constant instanceof Constant.Float floating) return floating.value;
    if (constant instanceof Constant.Zero && constant.getType() == Type.FLOAT) return 0.0f;
    return null;
  }

  private static Boolean compareInteger(String predicate, long lhs, long rhs) {
    return switch (predicate) {
      case "eq" -> lhs == rhs;
      case "ne" -> lhs != rhs;
      case "slt" -> lhs < rhs;
      case "sle" -> lhs <= rhs;
      case "sgt" -> lhs > rhs;
      case "sge" -> lhs >= rhs;
      default -> null;
    };
  }

  private static Long shiftLeft(Type type, long value, long amount) {
    int width = type == Type.I64 ? Long.SIZE : type == Type.INT ? Integer.SIZE : 1;
    if (amount < 0 || amount >= width) return null;
    return type == Type.I64 ? value << amount : (long) ((int) value << amount);
  }

  private static Long arithmeticShiftRight(Type type, long value, long amount) {
    int width = type == Type.I64 ? Long.SIZE : type == Type.INT ? Integer.SIZE : 1;
    if (amount < 0 || amount >= width) return null;
    return type == Type.I64 ? value >> amount : (long) ((int) value >> amount);
  }

  private static Boolean compareFloat(String predicate, float lhs, float rhs) {
    boolean unordered = Float.isNaN(lhs) || Float.isNaN(rhs);
    return switch (predicate) {
      case "oeq" -> !unordered && lhs == rhs;
      case "one" -> !unordered && lhs != rhs;
      case "olt" -> !unordered && lhs < rhs;
      case "ole" -> !unordered && lhs <= rhs;
      case "ogt" -> !unordered && lhs > rhs;
      case "oge" -> !unordered && lhs >= rhs;
      case "ueq" -> unordered || lhs == rhs;
      case "une" -> unordered || lhs != rhs;
      case "ult" -> unordered || lhs < rhs;
      case "ule" -> unordered || lhs <= rhs;
      case "ugt" -> unordered || lhs > rhs;
      case "uge" -> unordered || lhs >= rhs;
      default -> null;
    };
  }

  private static Constant convertConstant(
      Opcode opcode, Constant constant, Type source, Type destination) {
    return switch (opcode) {
      case ZEXT -> {
        Long value = integerValue(constant);
        if (value == null) yield null;
        long extended = source == Type.INT && destination == Type.I64
            ? Integer.toUnsignedLong((int) (long) value) : value;
        yield integerConstant(destination, extended);
      }
      case SEXT -> {
        Long value = integerValue(constant);
        if (value == null) yield null;
        long extended = source == Type.I1 ? (value == 0 ? 0 : -1)
            : source == Type.INT && destination == Type.I64 ? (int) (long) value : value;
        yield integerConstant(destination, extended);
      }
      case SITOFP -> {
        Long value = integerValue(constant);
        if (value != null && source == Type.I1 && value != 0) value = -1L;
        yield value == null || destination != Type.FLOAT
            ? null : Constant.floatConst((float) (long) value);
      }
      case FPTOSI -> {
        Float value = floatingValue(constant);
        if (value == null || !Float.isFinite(value)) yield null;
        double widened = value;
        if (destination == Type.I1 && (widened < -1.0 || widened >= 1.0)) yield null;
        if (destination == Type.INT
            && (widened < Integer.MIN_VALUE || widened >= 2147483648.0)) yield null;
        if (destination == Type.I64
            && (widened < Long.MIN_VALUE || widened >= 9223372036854775808.0)) yield null;
        yield integerConstant(destination, destination == Type.INT ? (int) widened : (long) widened);
      }
      default -> null;
    };
  }

  private static boolean sameConstant(Constant lhs, Constant rhs) {
    if (!lhs.getType().equals(rhs.getType())) return false;
    if (lhs == rhs) return true;
    if (lhs instanceof Constant.Zero) return isZero(rhs);
    if (rhs instanceof Constant.Zero) return isZero(lhs);
    if (lhs instanceof Constant.Int left && rhs instanceof Constant.Int right) {
      return integerValue(left).equals(integerValue(right));
    }
    if (lhs instanceof Constant.Float left && rhs instanceof Constant.Float right) {
      return Float.floatToRawIntBits(left.value) == Float.floatToRawIntBits(right.value);
    }
    if (lhs instanceof Constant.Vector left && rhs instanceof Constant.Vector right) {
      return sameElements(left.elements, right.elements);
    }
    if (lhs instanceof Constant.Array left && rhs instanceof Constant.Array right) {
      return sameElements(left.elements, right.elements);
    }
    return false;
  }

  private static boolean sameElements(List<Constant> lhs, List<Constant> rhs) {
    if (lhs.size() != rhs.size()) return false;
    for (int index = 0; index < lhs.size(); index++) {
      if (!sameConstant(lhs.get(index), rhs.get(index))) return false;
    }
    return true;
  }

  private static boolean isZero(Constant constant) {
    if (constant instanceof Constant.Zero) return true;
    if (constant instanceof Constant.Int integer) return integerValue(integer) == 0;
    if (constant instanceof Constant.Float floating) {
      return Float.floatToRawIntBits(floating.value) == 0;
    }
    if (constant instanceof Constant.Vector vector) return vector.elements.stream().allMatch(SCCP::isZero);
    if (constant instanceof Constant.Array array) return array.elements.stream().allMatch(SCCP::isZero);
    return false;
  }
}
