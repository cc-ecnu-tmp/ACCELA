package accela.ir;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * A basic block: a labeled sequence of instructions ending with a terminator.
 *
 * Extends Value so it can be used as an operand in branch instructions
 * (representing a branch target / label).
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

  public void addInstruction(Instruction inst) {
    inst.setParent(this);
    // Insert before terminator if block is already terminated
    if (isTerminated() && !inst.isTerminator()) {
      instructions.add(instructions.size() - 1, inst);
    } else {
      instructions.add(inst);
    }
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

  /** Get successor blocks */
  public List<BasicBlock> getSuccessors() {
    Instruction term = getTerminator();
    if (term == null) return Collections.emptyList();
    List<BasicBlock> succs = new ArrayList<>();
    switch (term.getOpcode()) {
      case BR:
        succs.add((BasicBlock) term.getOperand(0));
        break;
      case CONDBR:
        succs.add((BasicBlock) term.getOperand(1)); // true target
        succs.add((BasicBlock) term.getOperand(2)); // false target
        break;
      default:
        break;
    }
    return succs;
  }

  /** Get predecessor blocks */
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
