package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * A basic block: a labeled sequence of instructions ending with a terminator.
 *
 * Extends Value so it can be used as an operand in branch instructions
 * (representing a branch target / label).
 *
 * <p>The block owns its instruction list and is also the place where simple CFG queries such as
 * predecessor/successor discovery are performed.
 */
public class BasicBlock extends Value {
  private Function parent;
  private final List<Instruction> instructions = new ArrayList<>();

  public BasicBlock(String label) {
    super(Type.VOID, label);
  }

  public List<Instruction> getInstructions() {
    return Collections.unmodifiableList(instructions);
  }

  /**
   * Appends an instruction to the block.
   *
   * <p>If the block already has a terminator and the new instruction is not itself a terminator,
   * the instruction is inserted right before the existing terminator to keep the block structurally
   * valid.
   */
  public void addInstruction(Instruction inst) {
    inst.setParent(this);
    // Insert before terminator if block is already terminated
    if (isTerminated() && !inst.isTerminator()) {
      instructions.add(instructions.size() - 1, inst);
    } else {
      instructions.add(inst);
    }
  }

  /** Inserts an instruction at the beginning of the non-PHI region. */
  public void addInstructionToFront(Instruction inst) {
    inst.setParent(this);
    int insertAt = 0;
    while (insertAt < instructions.size()
        && instructions.get(insertAt).getOpcode() == Instruction.Opcode.PHI) {
      insertAt++;
    }
    instructions.add(insertAt, inst);
  }

  /** Inserts an instruction after the existing alloca prefix of the block. */
  public void addInstructionAfterAllocas(Instruction inst) {
    inst.setParent(this);
    int insertAt = 0;
    while (insertAt < instructions.size()
        && instructions.get(insertAt).getOpcode() == Instruction.Opcode.ALLOCA) {
      insertAt++;
    }
    instructions.add(insertAt, inst);
  }

  /**
   * Inserts an instruction immediately before another instruction already in this block.
   *
   * <p>This is primarily used by local rewrite passes such as SROA, where replacement operations
   * must preserve the original execution order of memory accesses.
   */
  public void insertInstructionBefore(Instruction before, Instruction inst) {
    int index = instructions.indexOf(before);
    if (index < 0) {
      throw new IllegalArgumentException("Instruction does not belong to this block");
    }
    inst.setParent(this);
    instructions.add(index, inst);
  }

  public void remove(Instruction inst) {
    instructions.remove(inst);
    inst.setParent(null);
  }

  public boolean isEmpty() {
    return instructions.isEmpty();
  }

  public Instruction getTerminator() {
    if (instructions.isEmpty()) return null;
    Instruction last = instructions.get(instructions.size() - 1);
    return last.isTerminator() ? last : null;
  }

  public boolean isTerminated() {
    return getTerminator() != null;
  }

  /** Returns CFG successors implied by the current terminator, if any. */
  public List<BasicBlock> getSuccessors() {
    Instruction term = getTerminator();
    if (term == null) return Collections.emptyList();
    return switch (term.getOpcode()) {
      case BR -> List.of((BasicBlock) term.getOperand(0));
      case CONDBR -> List.of(
          (BasicBlock) term.getOperand(1), (BasicBlock) term.getOperand(2));
      default -> Collections.emptyList();
    };
  }

  /** Returns CFG predecessors by scanning sibling blocks in the parent function. */
  public List<BasicBlock> getPredecessors() {
    if (parent == null) return Collections.emptyList();
    List<BasicBlock> preds = new ArrayList<>();
    for (BasicBlock bb : parent.getBlocks()) {
      if (bb.getSuccessors().contains(this)) preds.add(bb);
    }
    return preds;
  }

  public Function getParent() {
    return parent;
  }

  void setParent(Function func) {
    this.parent = func;
  }

  public String getLabel() {
    return name;
  }
}
