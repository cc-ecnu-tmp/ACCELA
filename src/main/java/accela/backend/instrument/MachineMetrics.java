package accela.backend.instrument;

import accela.backend.frame.StackSlotKind;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.EnumMap;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.Locale;

/** Stable generic counters for machine-level pass observations. */
public final class MachineMetrics {
  private long functions;
  private long blocks;
  private long instructions;
  private long virtualRegisters;
  private long stackSlots;
  private long spillSlots;
  private long spillReloads;
  private long spillStores;
  private long frameBytes;
  private final EnumMap<MachineOpcode, Long> opcodes = new EnumMap<>(MachineOpcode.class);

  public static MachineMetrics capture(MachineModule module) {
    MachineMetrics metrics = new MachineMetrics();
    for (MachineFunction function : module.getFunctions()) metrics.add(function);
    return metrics;
  }

  public static MachineMetrics capture(MachineFunction function) {
    MachineMetrics metrics = new MachineMetrics();
    metrics.add(function);
    return metrics;
  }

  public Map<String, Long> asMap() {
    LinkedHashMap<String, Long> result = new LinkedHashMap<>();
    result.put("functions", functions);
    result.put("blocks", blocks);
    result.put("instructions", instructions);
    result.put("virtual_registers", virtualRegisters);
    result.put("stack_slots", stackSlots);
    result.put("spill_slots", spillSlots);
    result.put("spill_reloads", spillReloads);
    result.put("spill_stores", spillStores);
    result.put("frame_bytes", frameBytes);
    for (MachineOpcode opcode : MachineOpcode.values()) {
      result.put("opcode." + opcode.name().toLowerCase(Locale.ROOT),
          opcodes.getOrDefault(opcode, 0L));
    }
    return Collections.unmodifiableMap(result);
  }

  private void add(MachineFunction function) {
    functions++;
    Set<VirtualRegister> registers = new HashSet<>(function.getArguments());
    stackSlots += function.getFrameInfo().getSlots().size();
    spillSlots += function.getFrameInfo().getSlots().stream()
        .filter(slot -> slot.getKind() == StackSlotKind.SPILL).count();
    frameBytes += function.getFrameInfo().getFrameSize();
    for (var block : function.getBlocks()) {
      blocks++;
      for (MachineInstr instruction : block.getInstructions()) {
        instructions++;
        opcodes.merge(instruction.getOpcode(), 1L, Long::sum);
        VirtualRegister destination = instruction.getDest();
        if (destination != null) {
          registers.add(destination);
          if (instruction.getOpcode() == MachineOpcode.LOAD
              && containsHint(destination, ".spill.reload")) spillReloads++;
        }
        for (var operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand vreg) registers.add(vreg.getRegister());
        }
        if (instruction.getOpcode() == MachineOpcode.STORE
            && instruction.getOperands().stream()
                .filter(VRegOperand.class::isInstance)
                .map(VRegOperand.class::cast)
                .anyMatch(vreg -> containsHint(vreg.getRegister(), ".spill.addr"))) {
          spillStores++;
        }
      }
    }
    virtualRegisters += registers.size();
  }

  private static boolean containsHint(VirtualRegister register, String marker) {
    return register.getHint() != null && register.getHint().contains(marker);
  }
}
