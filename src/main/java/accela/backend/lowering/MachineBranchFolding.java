package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterLocation;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Map;
import java.util.Set;

/** Removes jump-only blocks whose PHI copies coalesced after register allocation. */
public final class MachineBranchFolding {
  public boolean run(MachineFunction function, AllocationResult allocation) {
    Map<MachineBasicBlock, MachineBasicBlock> forwarding = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      if (block != function.getEntryBlock()) {
        MachineBasicBlock target = forwardingTarget(block, allocation);
        if (target != null && target != block) forwarding.put(block, target);
      }
    }
    if (forwarding.isEmpty()) return false;

    forwarding.replaceAll((block, target) -> resolve(target, forwarding));
    forwarding.entrySet().removeIf(entry -> entry.getValue() == null);
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (instruction.getOperands().get(index) instanceof BlockOperand target
              && forwarding.containsKey(target.getBlock())) {
            instruction.setOperand(index, new BlockOperand(forwarding.get(target.getBlock())));
          }
        }
      }
    }
    for (MachineBasicBlock block : forwarding.keySet()) function.removeBlock(block);
    return !forwarding.isEmpty();
  }

  private static MachineBasicBlock forwardingTarget(
      MachineBasicBlock block, AllocationResult allocation) {
    if (block.getInstructions().isEmpty()) return null;
    MachineInstr branch = block.getInstructions().getLast();
    if (branch.getOpcode() != MachineOpcode.BR || branch.getOperands().size() != 1) return null;
    for (MachineInstr instruction : block.getInstructions().subList(
        0, block.getInstructions().size() - 1)) {
      if (!isCoalescedCopy(instruction, allocation)) return null;
    }
    return ((BlockOperand) branch.getOperands().getFirst()).getBlock();
  }

  private static boolean isCoalescedCopy(
      MachineInstr instruction, AllocationResult allocation) {
    if (instruction.getOpcode() != MachineOpcode.MOVE
        || instruction.getDest() == null
        || instruction.getOperands().size() != 1
        || !(instruction.getOperands().getFirst() instanceof VRegOperand source)) return false;
    var destination = allocation.locationOf(instruction.getDest());
    var sourceLocation = allocation.locationOf(source.getRegister());
    return destination instanceof RegisterLocation destinationRegister
        && sourceLocation instanceof RegisterLocation sourceRegister
        && destinationRegister.getRegister().getName()
            .equals(sourceRegister.getRegister().getName());
  }

  private static MachineBasicBlock resolve(
      MachineBasicBlock target, Map<MachineBasicBlock, MachineBasicBlock> forwarding) {
    Set<MachineBasicBlock> visited = Collections.newSetFromMap(new IdentityHashMap<>());
    while (forwarding.containsKey(target)) {
      if (!visited.add(target)) return null;
      target = forwarding.get(target);
    }
    return target;
  }
}
