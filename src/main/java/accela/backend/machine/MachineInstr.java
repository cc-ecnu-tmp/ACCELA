package accela.backend.machine;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class MachineInstr {
  private final MachineOpcode opcode;
  private final VirtualRegister dest;
  private final List<MachineOperand> operands = new ArrayList<>();
  private final List<MachineType> operandTypes = new ArrayList<>();
  private MachineType type = MachineType.VOID;
  private String predicate;
  private String callee;
  private boolean coalescable = true;
  private RVVConfig rvvConfig;
  private VCIXInfo vcixInfo;

  public MachineInstr(MachineOpcode opcode, VirtualRegister dest) {
    this.opcode = opcode;
    this.dest = dest;
  }

  public MachineOpcode getOpcode() {
    return opcode;
  }

  public VirtualRegister getDest() {
    return dest;
  }

  public List<MachineOperand> getOperands() {
    return Collections.unmodifiableList(operands);
  }

  public MachineInstr addOperand(MachineOperand operand) {
    return addOperand(operand, null);
  }

  public MachineInstr addOperand(MachineOperand operand, MachineType type) {
    operands.add(operand);
    operandTypes.add(type);
    return this;
  }

  public void setOperand(int index, MachineOperand operand) {
    operands.set(index, operand);
  }

  public MachineType getOperandType(int index) {
    return operandTypes.get(index);
  }

  public void setOperandType(int index, MachineType type) {
    operandTypes.set(index, type);
  }

  public MachineType getType() {
    return type;
  }

  public void setType(MachineType type) {
    this.type = type;
  }

  public String getPredicate() {
    return predicate;
  }

  public void setPredicate(String predicate) {
    this.predicate = predicate;
  }

  public String getCallee() {
    return callee;
  }

  public void setCallee(String callee) {
    this.callee = callee;
  }

  public boolean isCoalescable() {
    return coalescable;
  }

  public void setCoalescable(boolean coalescable) {
    this.coalescable = coalescable;
  }

  public RVVConfig getRVVConfig() {
    return rvvConfig;
  }

  public void setRVVConfig(RVVConfig rvvConfig) {
    this.rvvConfig = rvvConfig;
  }

  public VCIXInfo getVCIXInfo() {
    return vcixInfo;
  }

  public void setVCIXInfo(VCIXInfo vcixInfo) {
    this.vcixInfo = vcixInfo;
  }

  public boolean isTerminator() {
    return opcode == MachineOpcode.BR || opcode == MachineOpcode.CONDBR || opcode == MachineOpcode.RET;
  }
}
