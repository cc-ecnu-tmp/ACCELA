package accela.backend.machine;

import accela.ir.BasicBlock;
import accela.ir.Function;
import java.util.ArrayList;
import java.util.List;

public final class MachineBasicBlock {
  private final String label;
  private final List<MachineInstr> instructions = new ArrayList<>();
  private Function sourceFunction;
  private BasicBlock sourceBlock;

  public MachineBasicBlock(String label) {
    this.label = label;
  }

  public String getLabel() {
    return label;
  }

  public List<MachineInstr> getInstructions() {
    return instructions;
  }

  public void addInstruction(MachineInstr instr) {
    instructions.add(instr);
  }

  public void insertBeforeTerminator(MachineInstr instr) {
    if (!instructions.isEmpty() && instructions.get(instructions.size() - 1).isTerminator()) {
      instructions.add(instructions.size() - 1, instr);
    } else {
      instructions.add(instr);
    }
  }

  public Function getSourceFunction() {
    return sourceFunction;
  }

  public void setSourceFunction(Function sourceFunction) {
    this.sourceFunction = sourceFunction;
  }

  public BasicBlock getSourceBlock() {
    return sourceBlock;
  }

  public void setSourceBlock(BasicBlock sourceBlock) {
    this.sourceBlock = sourceBlock;
  }
}
