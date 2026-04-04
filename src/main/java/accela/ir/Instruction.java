package accela.ir;

import java.util.ArrayList;
import java.util.List;

/**
 * An IR instruction. Extends Value because most instructions define a result
 * that can be used as an operand in other instructions.
 *
 * Instructions with void results (store, br, ret void) have type VOID.
 *
 * <p>Besides the opcode and operand list, this class stores a small amount of opcode-specific
 * metadata such as compare predicates, alloca element types, GEP source types, and direct call
 * callees.
 */
public class Instruction extends Value {

  /** Supported opcodes in the project IR. */
  public enum Opcode {
    // Arithmetic
    ADD, SUB, MUL, SDIV, SREM,
    // Arithmetic
    FADD, FSUB, FMUL, FDIV, FNEG,
    // Comparison
    ICMP, FCMP,
    // Memory
    ALLOCA, LOAD, STORE, GEP,
    // Control flow
    BR, CONDBR, RET,
    // Conversion
    ZEXT, SEXT, SITOFP, FPTOSI,
    // Other
    CALL, PHI, XOR;

    public boolean isTerminator() {
      return this == BR || this == CONDBR || this == RET;
    }
  }

  private final Opcode opcode;
  private final List<Use> operands = new ArrayList<>();
  private BasicBlock parent;

  private String predicate;     // for ICMP/FCMP: "eq", "slt", "olt", etc.
  private Type allocatedType;   // for ALLOCA: the type being allocated
  private Type gepSourceType;   // for GEP: the source element type
  private boolean gepInbounds;  // for GEP: inbounds flag
  private Function callee;      // for CALL: the called function (null for indirect)

  Instruction(Opcode opcode, Type resultType, Value... operandValues) {
    super(resultType, null);
    this.opcode = opcode;
    for (int i = 0; i < operandValues.length; i++) {
      operands.add(new Use(operandValues[i], this, i));
    }
  }

  /** Creates an empty PHI instruction whose incoming pairs can be filled later. */
  public static Instruction createPhi(Type resultType) {
    return new Instruction(Opcode.PHI, resultType);
  }

  public Opcode getOpcode() {
    return opcode;
  }

  public BasicBlock getParent() {
    return parent;
  }

  void setParent(BasicBlock bb) {
    this.parent = bb;
  }

  public int getNumOperands() {
    return operands.size();
  }

  /** Returns the operand value currently stored in the given operand slot. */
  public Value getOperand(int index) {
    return operands.get(index).getValue();
  }

  /** Rewrites one operand slot while keeping use-lists consistent. */
  public void setOperand(int index, Value newValue) {
    operands.get(index).setValue(newValue);
  }

  /** Appends a new operand to the instruction. */
  public void addOperand(Value value) {
    operands.add(new Use(value, this, operands.size()));
  }

  /** Removes all operands. */
  public void clearAllOperands() {
    for (Use use : new ArrayList<>(operands)) {
      use.drop();
    }
    operands.clear();
  }

  public String getPredicate() {
    return predicate;
  }

  public void setPredicate(String predicate) {
    this.predicate = predicate;
  }

  public Type getAllocatedType() {
    return allocatedType;
  }

  public void setAllocatedType(Type type) {
    this.allocatedType = type;
  }

  public Type getGepSourceType() {
    return gepSourceType;
  }

  public void setGepSourceType(Type type) {
    this.gepSourceType = type;
  }

  public boolean isGepInbounds() {
    return gepInbounds;
  }

  public void setGepInbounds(boolean inbounds) {
    this.gepInbounds = inbounds;
  }

  public Function getCallee() {
    return callee;
  }

  public void setCallee(Function callee) {
    this.callee = callee;
  }

  /** Whether this instruction is a terminator. */
  public boolean isTerminator() {
    return opcode.isTerminator();
  }

  /** Whether this instruction produces a value. */
  public boolean hasResult() {
    return type != Type.VOID;
  }

  /** Drop all operand references. */
  public void dropAllReferences() {
    for (Use use : operands) use.drop();
  }

  /**
   * Remove this instruction from its parent basic block.
   * Also drops all operand references.
   */
  public void eraseFromParent() {
    dropAllReferences();
    if (parent != null) parent.remove(this);
  }
}
