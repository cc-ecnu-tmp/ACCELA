package accela.backend.machine;

import accela.backend.frame.MachineFrameInfo;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.IdentityHashMap;
import java.util.Map;
import accela.backend.frame.StackSlot;

public final class MachineFunction {
  private final String name;
  private final MachineType returnType;
  private final List<MachineBasicBlock> blocks = new ArrayList<>();
  private MachineFrameInfo frameInfo = new MachineFrameInfo();
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

  public void reorderBlocks(List<MachineBasicBlock> order) {
    if (order.size() != blocks.size()
        || !order.containsAll(blocks)
        || !blocks.containsAll(order)) {
      throw new IllegalArgumentException("block order must contain every function block once");
    }
    blocks.clear();
    blocks.addAll(order);
  }

  public void removeBlock(MachineBasicBlock block) {
    if (block == getEntryBlock()) throw new IllegalArgumentException("cannot remove entry block");
    blocks.remove(block);
  }

  public MachineBasicBlock getEntryBlock() {
    return blocks.isEmpty() ? null : blocks.get(0);
  }

  /** Produces a transaction-safe copy with no shared mutable Machine IR or frame state. */
  public MachineFunction deepCopy() {
    MachineFunction copy = new MachineFunction(name, returnType);
    Map<VirtualRegister, VirtualRegister> registers = new java.util.HashMap<>();
    Map<MachineBasicBlock, MachineBasicBlock> blockMap = new IdentityHashMap<>();
    Map<StackSlot, StackSlot> slotMap = copy.frameInfo.copyFrom(frameInfo);
    for (MachineBasicBlock block : blocks) {
      MachineBasicBlock blockCopy = copy.addBlock(block.getLabel());
      blockCopy.setSourceFunction(block.getSourceFunction());
      blockCopy.setSourceBlock(block.getSourceBlock());
      blockMap.put(block, blockCopy);
    }
    for (int argumentIndex = 0; argumentIndex < arguments.size(); argumentIndex++) {
      VirtualRegister argument = cloneRegister(arguments.get(argumentIndex), registers);
      copy.addArgument(argument, argumentTypes.get(argumentIndex));
    }
    for (MachineBasicBlock block : blocks) {
      MachineBasicBlock blockCopy = blockMap.get(block);
      for (MachineInstr instruction : block.getInstructions()) {
        VirtualRegister destination = instruction.getDest() == null
            ? null : cloneRegister(instruction.getDest(), registers);
        MachineInstr instructionCopy = new MachineInstr(instruction.getOpcode(), destination);
        instructionCopy.setType(instruction.getType());
        instructionCopy.setPredicate(instruction.getPredicate());
        instructionCopy.setCallee(instruction.getCallee());
        instructionCopy.setCoalescable(instruction.isCoalescable());
        for (int operandIndex = 0; operandIndex < instruction.getOperands().size(); operandIndex++) {
          instructionCopy.addOperand(cloneOperand(instruction.getOperands().get(operandIndex),
              registers, blockMap, slotMap), instruction.getOperandType(operandIndex));
        }
        blockCopy.addInstruction(instructionCopy);
      }
    }
    copy.nextVRegId = nextVRegId;
    return copy;
  }

  /** Atomically replaces this function with an isolated copy of a selected candidate. */
  public void replaceWith(MachineFunction selected) {
    if (!name.equals(selected.name) || returnType != selected.returnType) {
      throw new IllegalArgumentException("candidate function identity mismatch");
    }
    MachineFunction copy = selected.deepCopy();
    blocks.clear();
    blocks.addAll(copy.blocks);
    arguments.clear();
    arguments.addAll(copy.arguments);
    argumentTypes.clear();
    argumentTypes.addAll(copy.argumentTypes);
    frameInfo = copy.frameInfo;
    nextVRegId = copy.nextVRegId;
  }

  private static VirtualRegister cloneRegister(
      VirtualRegister register, Map<VirtualRegister, VirtualRegister> registers) {
    return registers.computeIfAbsent(register,
        value -> new VirtualRegister(value.getId(), value.getType(), value.getHint()));
  }

  private static MachineOperand cloneOperand(
      MachineOperand operand,
      Map<VirtualRegister, VirtualRegister> registers,
      Map<MachineBasicBlock, MachineBasicBlock> blocks,
      Map<StackSlot, StackSlot> slots) {
    if (operand instanceof VRegOperand value) {
      return new VRegOperand(cloneRegister(value.getRegister(), registers));
    }
    if (operand instanceof BlockOperand value) return new BlockOperand(blocks.get(value.getBlock()));
    if (operand instanceof StackSlotOperand value) {
      StackSlot slot = slots.get(value.getSlot());
      if (slot == null) throw new IllegalStateException("machine operand refers to an unknown stack slot");
      return new StackSlotOperand(slot);
    }
    if (operand instanceof ImmOperand value) return new ImmOperand(value.getValue());
    if (operand instanceof FloatImmOperand value) return new FloatImmOperand(value.getValue());
    if (operand instanceof SymbolOperand value) return new SymbolOperand(value.getSymbol());
    if (operand instanceof PhysicalRegOperand value) return new PhysicalRegOperand(value.getRegister());
    throw new IllegalStateException("unsupported machine operand " + operand.getClass().getName());
  }
}
