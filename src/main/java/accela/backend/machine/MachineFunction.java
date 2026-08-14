package accela.backend.machine;

import accela.backend.frame.MachineFrameInfo;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class MachineFunction {
  private final String name;
  private final MachineType returnType;
  private final List<MachineBasicBlock> blocks = new ArrayList<>();
  private final MachineFrameInfo frameInfo = new MachineFrameInfo();
  private final List<VirtualRegister> arguments = new ArrayList<>();
  private final List<MachineType> argumentTypes = new ArrayList<>();
  private final Set<KnownSext32> knownSext32 = new HashSet<>();
  private final Map<VirtualRegister, MachineInstr> rematerializationRecipes = new HashMap<>();
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

  /** Marks a virtual register whose 64-bit value is known to be sign-extended from i32. */
  public void markKnownSext32(VirtualRegister register) {
    if (register == null) throw new IllegalArgumentException("register must be present");
    knownSext32.add(new KnownSext32(register));
  }

  public boolean isKnownSext32(VirtualRegister register) {
    return knownSext32.contains(new KnownSext32(register));
  }

  public Set<KnownSext32> knownSext32Properties() {
    return Collections.unmodifiableSet(knownSext32);
  }

  /** Records a side-effect-free definition that the spill rewriter may rebuild at a use. */
  public void markRematerializable(VirtualRegister register, MachineInstr recipe) {
    if (register == null || recipe == null || recipe.getDest() != register) {
      throw new IllegalArgumentException("rematerialization recipe does not define the register");
    }
    rematerializationRecipes.put(register, recipe);
  }

  public Map<VirtualRegister, MachineInstr> rematerializationRecipes() {
    return Collections.unmodifiableMap(rematerializationRecipes);
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
}
