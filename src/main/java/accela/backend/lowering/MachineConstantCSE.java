package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Shares profitable constant materializations without extending them beyond adjacent blocks. */
public final class MachineConstantCSE {
  public boolean run(MachineFunction function) {
    boolean changed = shareWithUniqueSuccessors(function);
    for (MachineBasicBlock block : function.getBlocks()) {
      for (var entry : collectRepeatedStoreValues(block).entrySet()) {
        List<Use> uses = entry.getValue();
        if (uses.size() < 2) continue;
        materialize(function, block, entry.getKey(), uses);
        changed = true;
      }
      for (var entry : collectRepeatedAdds(block).entrySet()) {
        List<Use> uses = entry.getValue();
        if (uses.size() < 3) continue;
        materialize(function, block, entry.getKey(), uses);
        changed = true;
      }
    }
    return changed;
  }

  private static Map<Key, List<Use>> collectRepeatedStoreValues(MachineBasicBlock block) {
    Map<Key, List<Use>> uses = new LinkedHashMap<>();
    for (MachineInstr instruction : block.getInstructions()) {
      if (instruction.getOpcode() != MachineOpcode.STORE
          || !instruction.getType().isIntegerLike()
          || !(instruction.getOperands().getFirst() instanceof ImmOperand immediate)
          || immediate.getValue() == 0) continue;
      Key key = new Key(instruction.getType(), immediate.getValue());
      uses.computeIfAbsent(key, ignored -> new ArrayList<>())
          .add(new Use(instruction, 0));
    }
    return uses;
  }

  private static boolean shareWithUniqueSuccessors(MachineFunction function) {
    Map<MachineBasicBlock, Integer> predecessorCounts = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineBasicBlock successor : successors(block)) {
        predecessorCounts.merge(successor, 1, Integer::sum);
      }
    }

    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      Map<Key, List<Use>> local = collectExpensiveUses(block);
      for (var entry : local.entrySet()) {
        List<Use> uses = new ArrayList<>(entry.getValue());
        for (MachineBasicBlock successor : successors(block)) {
          if (predecessorCounts.getOrDefault(successor, 0) != 1) continue;
          uses.addAll(collectExpensiveUses(successor)
              .getOrDefault(entry.getKey(), List.of()));
        }
        if (uses.size() == entry.getValue().size()) continue;
        materialize(function, block, entry.getKey(), uses);
        changed = true;
      }
    }
    return changed;
  }

  private static Map<Key, List<Use>> collectRepeatedAdds(MachineBasicBlock block) {
    Map<Key, List<Use>> uses = new LinkedHashMap<>();
    for (MachineInstr instruction : block.getInstructions()) {
      if (instruction.getOpcode() != MachineOpcode.ADD) continue;
      for (int index = 0; index < instruction.getOperands().size(); index++) {
        if (instruction.getOperands().get(index) instanceof ImmOperand immediate
            && !fitsSigned12(immediate.getValue())) {
          Key key = new Key(instruction.getType(), immediate.getValue());
          uses.computeIfAbsent(key, ignored -> new ArrayList<>())
              .add(new Use(instruction, index));
        }
      }
    }
    return uses;
  }

  private static Map<Key, List<Use>> collectExpensiveUses(MachineBasicBlock block) {
    Map<Key, List<Use>> uses = new LinkedHashMap<>();
    for (MachineInstr instruction : block.getInstructions()) {
      MachineType type = constantType(instruction);
      if (type == null) continue;
      for (int index = 0; index < instruction.getOperands().size(); index++) {
        if (instruction.getOperands().get(index) instanceof ImmOperand immediate
            && isExpensive(immediate.getValue())) {
          Key key = new Key(type, immediate.getValue());
          uses.computeIfAbsent(key, ignored -> new ArrayList<>())
              .add(new Use(instruction, index));
        }
      }
    }
    return uses;
  }

  private static MachineType constantType(MachineInstr instruction) {
    return switch (instruction.getOpcode()) {
      case ICMP, CONDBR, SMULH -> MachineType.I32;
      case ADD, SUB, MUL, AND, XOR ->
          instruction.getType().isIntegerLike() ? instruction.getType() : null;
      default -> null;
    };
  }

  private static List<MachineBasicBlock> successors(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return List.of();
    return block.getInstructions().getLast().getOperands().stream()
        .filter(BlockOperand.class::isInstance)
        .map(BlockOperand.class::cast)
        .map(BlockOperand::getBlock)
        .distinct()
        .toList();
  }

  private static void materialize(
      MachineFunction function, MachineBasicBlock block, Key key, List<Use> uses) {
    VirtualRegister register = function.createVirtualRegister(key.type(), "constant");
    MachineInstr constant = new MachineInstr(MachineOpcode.CONST_INT, register);
    constant.setType(key.type());
    constant.addOperand(new ImmOperand(key.value()));

    MachineInstr firstUse = uses.getFirst().instruction();
    block.getInstructions().add(block.getInstructions().indexOf(firstUse), constant);
    for (Use use : uses) {
      use.instruction().setOperand(use.index(), new VRegOperand(register));
    }
  }

  private static boolean fitsSigned12(long value) {
    return value >= -2048 && value <= 2047;
  }

  private static boolean isExpensive(long value) {
    return !fitsSigned12(value) && (value & 0xfff) != 0;
  }

  private record Key(MachineType type, long value) {}
  private record Use(MachineInstr instruction, int index) {}
}
