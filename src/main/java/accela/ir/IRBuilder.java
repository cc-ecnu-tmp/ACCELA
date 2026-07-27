package accela.ir;

import accela.ir.Instruction.Opcode;

/**
 * Builder API for constructing IR instructions. Manages the current insertion
 * point and automatically handles use-def chain maintenance.
 *
 * <p>Usage:
 *
 *   IRBuilder b = new IRBuilder();
 *   b.setInsertPoint(entryBlock);
 *   Instruction add = b.createAdd(lhs, rhs);
 *   b.createStore(add, ptr);
 *
 * <p>The builder does not perform semantic validation on its own; callers are expected to supply
 * type-correct operands and a valid insertion point.
 */
public class IRBuilder {
  private BasicBlock insertBB;
  private Instruction insertBefore;

  public IRBuilder() {}

  public IRBuilder(BasicBlock bb) {
    this.insertBB = bb;
  }

  public void setInsertPoint(BasicBlock bb) {
    this.insertBB = bb;
    this.insertBefore = null;
  }

  /** Inserts subsequent instructions immediately before {@code inst}. */
  public void setInsertPointBefore(Instruction inst) {
    this.insertBB = inst.getParent();
    this.insertBefore = inst;
  }

  public BasicBlock getInsertBlock() {
    return insertBB;
  }

  /** Returns whether the current insertion block already ends in a terminator. */
  public boolean isTerminated() {
    return insertBB != null && insertBB.isTerminated();
  }

  /** Inserts an instruction into the current block and returns it for immediate use. */
  private Instruction insert(Instruction inst) {
    if (insertBefore != null) {
      insertBB.insertInstructionBefore(insertBefore, inst);
    } else {
      insertBB.addInstruction(inst);
    }
    return inst;
  }

  public Instruction createAdd(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.ADD, Type.INT, lhs, rhs));
  }

  public Instruction createSub(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.SUB, Type.INT, lhs, rhs));
  }

  public Instruction createMul(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.MUL, Type.INT, lhs, rhs));
  }

  /** Returns the high 32 bits of the signed i32-by-i32 product. */
  public Instruction createSMulH(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.SMULH, Type.INT, lhs, rhs));
  }

  public Instruction createSDiv(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.SDIV, Type.INT, lhs, rhs));
  }

  public Instruction createSRem(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.SREM, Type.INT, lhs, rhs));
  }

  public Instruction createShl(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.SHL, Type.INT, lhs, rhs));
  }

  public Instruction createAShr(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.ASHR, Type.INT, lhs, rhs));
  }

  public Instruction createFAdd(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.FADD, Type.FLOAT, lhs, rhs));
  }

  public Instruction createFSub(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.FSUB, Type.FLOAT, lhs, rhs));
  }

  public Instruction createFMul(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.FMUL, Type.FLOAT, lhs, rhs));
  }

  public Instruction createFDiv(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.FDIV, Type.FLOAT, lhs, rhs));
  }

  public Instruction createFNeg(Value operand) {
    return insert(new Instruction(Opcode.FNEG, Type.FLOAT, operand));
  }

  public Instruction createICmp(String pred, Value lhs, Value rhs) {
    Instruction inst = new Instruction(Opcode.ICMP, Type.I1, lhs, rhs);
    inst.setPredicate(pred);
    return insert(inst);
  }

  public Instruction createFCmp(String pred, Value lhs, Value rhs) {
    Instruction inst = new Instruction(Opcode.FCMP, Type.I1, lhs, rhs);
    inst.setPredicate(pred);
    return insert(inst);
  }

  public Instruction createAlloca(Type allocType) {
    Instruction inst = new Instruction(Opcode.ALLOCA, Type.PTR);
    inst.setAllocatedType(allocType);
    return insert(inst);
  }

  /** Creates an alloca directly in the entry block, regardless of the current insertion point. */
  public Instruction createAllocaInEntry(Type allocType, BasicBlock entryBB) {
    Instruction inst = new Instruction(Opcode.ALLOCA, Type.PTR);
    inst.setAllocatedType(allocType);
    entryBB.addInstructionAfterAllocas(inst);
    return inst;
  }

  public Instruction createLoad(Type type, Value ptr) {
    return insert(new Instruction(Opcode.LOAD, type, ptr));
  }

  public Instruction createStore(Value val, Value ptr) {
    return insert(new Instruction(Opcode.STORE, Type.VOID, val, ptr));
  }

  /**
   * Create a GEP instruction.
   *
   * @param sourceElemType the type being indexed into
   * @param ptr            the base pointer
   * @param indices        the index values
   * @param inbounds       whether to use inbounds
   */
  public Instruction createGEP(Type sourceElemType, Value ptr, Value[] indices, boolean inbounds) {
    Value[] operands = new Value[1 + indices.length];
    operands[0] = ptr;
    System.arraycopy(indices, 0, operands, 1, indices.length);
    Instruction inst = new Instruction(Opcode.GEP, Type.PTR, operands);
    inst.setGepSourceType(sourceElemType);
    inst.setGepInbounds(inbounds);
    return insert(inst);
  }

  public Instruction createBr(BasicBlock target) {
    return insert(new Instruction(Opcode.BR, Type.VOID, target));
  }

  public Instruction createCondBr(Value cond, BasicBlock ifTrue, BasicBlock ifFalse) {
    return insert(new Instruction(Opcode.CONDBR, Type.VOID, cond, ifTrue, ifFalse));
  }

  public Instruction createRet(Value val) {
    return insert(new Instruction(Opcode.RET, Type.VOID, val));
  }

  public Instruction createRetVoid() {
    return insert(new Instruction(Opcode.RET, Type.VOID));
  }

  public Instruction createZExt(Value val, Type destType) {
    return insert(new Instruction(Opcode.ZEXT, destType, val));
  }

  public Instruction createSExt(Value val, Type destType) {
    return insert(new Instruction(Opcode.SEXT, destType, val));
  }

  public Instruction createSIToFP(Value val, Type destType) {
    return insert(new Instruction(Opcode.SITOFP, destType, val));
  }

  public Instruction createFPToSI(Value val, Type destType) {
    return insert(new Instruction(Opcode.FPTOSI, destType, val));
  }

  public Instruction createXor(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.XOR, lhs.getType(), lhs, rhs));
  }

  public Instruction createAnd(Value lhs, Value rhs) {
    return insert(new Instruction(Opcode.AND, lhs.getType(), lhs, rhs));
  }

  /** Clones a side-effect-free binary expression while preserving its result type. */
  public Instruction createBinary(Opcode opcode, Value lhs, Value rhs) {
    return switch (opcode) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR,
          FADD, FSUB, FMUL, FDIV ->
          insert(new Instruction(opcode, lhs.getType(), lhs, rhs));
      default -> throw new IllegalArgumentException("not a binary expression: " + opcode);
    };
  }

  /**
   * Create a function call.
   *
   * @param callee   the called function
   * @param retType  return type
   * @param args     argument values
   */
  public Instruction createCall(Function callee, Type retType, Value... args) {
    Instruction inst = new Instruction(Opcode.CALL, retType, args);
    inst.setCallee(callee);
    return insert(inst);
  }

  public Instruction createPhi(Type type) {
    return insert(new Instruction(Opcode.PHI, type));
  }
}
