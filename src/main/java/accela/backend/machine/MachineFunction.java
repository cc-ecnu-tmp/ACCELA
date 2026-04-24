package accela.backend.machine;

import accela.backend.frame.MachineFrameInfo;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class MachineFunction {
  private final String name;
  private final MachineType returnType;
  private final List<MachineBasicBlock> blocks = new ArrayList<>();
  private final MachineFrameInfo frameInfo = new MachineFrameInfo();
  private final List<VirtualRegister> arguments = new ArrayList<>();
  private final List<MachineType> argumentTypes = new ArrayList<>();
  private int nextVRegId = 0;

  public MachineFunction(String name, MachineType returnType) {
    this.name = name;
    this.returnType = returnType;
  }

  public String getName() {
    return name;
  }

  public MachineType getReturnType() {
    return returnType;
  }

  public MachineFrameInfo getFrameInfo() {
    return frameInfo;
  }

  public VirtualRegister createVirtualRegister(MachineType type, String hint) {
    return new VirtualRegister(nextVRegId++, type, hint);
  }

  public void addArgument(VirtualRegister reg, MachineType type) {
    arguments.add(reg);
    argumentTypes.add(type);
  }

  public List<VirtualRegister> getArguments() {
    return Collections.unmodifiableList(arguments);
  }

  public List<MachineType> getArgumentTypes() {
    return Collections.unmodifiableList(argumentTypes);
  }

  public MachineBasicBlock addBlock(String label) {
    MachineBasicBlock block = new MachineBasicBlock(label);
    blocks.add(block);
    return block;
  }

  public List<MachineBasicBlock> getBlocks() {
    return Collections.unmodifiableList(blocks);
  }

  public MachineBasicBlock getEntryBlock() {
    return blocks.isEmpty() ? null : blocks.get(0);
  }
}
