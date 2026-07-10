package accela.backend.target;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Materializes repeatedly used constants once in the machine entry block. */
final class MachineConstantPool {
  private final MachineFunction function;
  private final Map<Key, Integer> useCounts = new HashMap<>();
  private final Map<Key, VirtualRegister> shared = new HashMap<>();

  MachineConstantPool(MachineFunction function) {
    this.function = function;
  }

  void count(MachineType type, long value) {
    useCounts.merge(new Key(type, value), 1, Integer::sum);
  }

  VRegOperand materialize(
      MachineType type, long value, List<MachineInstr> localInstructions) {
    Key key = new Key(type, value);
    if (useCounts.getOrDefault(key, 0) < 2) {
      return new VRegOperand(createConstant(type, value, localInstructions));
    }
    VirtualRegister register = shared.computeIfAbsent(key, ignored -> {
      VirtualRegister result = function.createVirtualRegister(type, "constant");
      MachineInstr constant = constantInstruction(result, type, value);
      function.getEntryBlock().insertBeforeTerminator(constant);
      return result;
    });
    return new VRegOperand(register);
  }

  private VirtualRegister createConstant(
      MachineType type, long value, List<MachineInstr> instructions) {
    VirtualRegister result = function.createVirtualRegister(type, "constant");
    instructions.add(constantInstruction(result, type, value));
    return result;
  }

  private static MachineInstr constantInstruction(
      VirtualRegister result, MachineType type, long value) {
    MachineInstr constant = new MachineInstr(MachineOpcode.CONST_INT, result);
    constant.setType(type);
    constant.addOperand(new ImmOperand(value));
    return constant;
  }

  private record Key(MachineType type, long value) {}
}
