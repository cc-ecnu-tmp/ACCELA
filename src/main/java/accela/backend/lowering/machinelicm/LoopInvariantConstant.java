package accela.backend.lowering;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Uses of one immediate whose RISC-V materialization costs multiple instructions. */
record LoopInvariantConstant(MachineType type, long value, List<Use> uses) {
  static List<LoopInvariantConstant> collect(MachineLoopInfo loop) {
    Map<Key, List<Use>> uses = new LinkedHashMap<>();
    loop.blocks().stream().flatMap(block -> block.getInstructions().stream())
        .forEach(instruction -> collectInstructionUses(instruction, uses));
    return uses.entrySet().stream()
        .map(entry ->
            new LoopInvariantConstant(
                entry.getKey().type(), entry.getKey().value(), entry.getValue()))
        .toList();
  }

  void replaceWith(VirtualRegister register) {
    for (Use use : uses) {
      use.instruction().setOperand(use.index(), new VRegOperand(register));
    }
  }

  boolean isCheapBranchConstant() {
    return uses.stream()
        .allMatch(use -> use.instruction().getOpcode() == MachineOpcode.CONDBR);
  }

  boolean isPointerAdd() {
    return type == MachineType.PTR && uses.stream()
        .allMatch(use -> use.instruction().getOpcode() == MachineOpcode.ADD);
  }

  private static void collectInstructionUses(
      MachineInstr instruction, Map<Key, List<Use>> uses) {
    MachineType type = constantType(instruction);
    if (type == null) return;
    for (int index = 0; index < instruction.getOperands().size(); index++) {
      if (instruction.getOperands().get(index) instanceof ImmOperand immediate
          && shouldHoist(instruction, immediate.getValue())) {
        uses.computeIfAbsent(new Key(type, immediate.getValue()), ignored -> new ArrayList<>())
            .add(new Use(instruction, index));
      }
    }
  }

  private static MachineType constantType(MachineInstr instruction) {
    return switch (instruction.getOpcode()) {
      case ICMP, SMULH, CONDBR -> MachineType.I32;
      case ADD, SUB, MUL, AND, XOR ->
          instruction.getType().isIntegerLike() ? instruction.getType() : null;
      default -> null;
    };
  }

  private static boolean shouldHoist(MachineInstr instruction, long value) {
    // RISC-V branches cannot compare an immediate other than zero.
    if (instruction.getOpcode() == MachineOpcode.CONDBR) return value != 0;
    if (instruction.getOpcode() == MachineOpcode.ADD
        && instruction.getType() == MachineType.PTR) {
      return value < -2048 || value > 2047;
    }
    return isExpensive(value);
  }

  private static boolean isExpensive(long value) {
    return (value < -2048 || value > 2047) && (value & 0xfff) != 0;
  }

  private record Key(MachineType type, long value) {}
  record Use(MachineInstr instruction, int index) {}
}
