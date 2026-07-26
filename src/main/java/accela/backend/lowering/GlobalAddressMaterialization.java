package accela.backend.lowering;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.LivenessAnalysis;
import accela.backend.regalloc.TargetRegisterInfo;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

/** Reuses frequently referenced global addresses when caller-saved pressure permits. */
public final class GlobalAddressMaterialization {
  private static final int MIN_USES = 3;
  private final TargetRegisterInfo registers = new TargetRegisterInfo();

  public boolean run(MachineFunction function) {
    MachineBasicBlock entry = function.getEntryBlock();
    if (entry == null || hasCallsOutsideEntry(function, entry)) return false;

    Map<String, Integer> counts = new LinkedHashMap<>();
    Set<String> entryUses = new LinkedHashSet<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (var operand : instruction.getOperands()) {
          if (!(operand instanceof SymbolOperand symbol)) continue;
          counts.merge(symbol.getSymbol(), 1, Integer::sum);
          if (block == entry) entryUses.add(symbol.getSymbol());
        }
      }
    }

    // Keep these long-lived addresses within the allocator's real caller-saved pool,
    // avoiding both spills and new callee-save traffic.
    int available =
        registers.callerSavedRegisters(MachineType.PTR).size() - peakIntegerLiveness(function);
    Map<String, VirtualRegister> addresses = new LinkedHashMap<>();
    counts.entrySet().stream()
        .filter(symbol -> symbol.getValue() >= MIN_USES)
        .filter(symbol -> !entryUses.contains(symbol.getKey()))
        .sorted(Map.Entry.<String, Integer>comparingByValue(Comparator.reverseOrder()))
        .limit(Math.max(0, available))
        .forEach(symbol -> addresses.put(
            symbol.getKey(), function.createVirtualRegister(MachineType.PTR, "global.addr")));
    if (addresses.isEmpty()) return false;

    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (!(instruction.getOperands().get(index) instanceof SymbolOperand symbol)) continue;
          VirtualRegister address = addresses.get(symbol.getSymbol());
          if (address != null) instruction.setOperand(index, new VRegOperand(address));
        }
      }
    }
    for (var address : addresses.entrySet()) {
      MachineInstr materialize = new MachineInstr(MachineOpcode.MOVE, address.getValue());
      materialize.setType(MachineType.PTR);
      materialize.addOperand(new SymbolOperand(address.getKey()));
      entry.insertBeforeTerminator(materialize);
    }
    return true;
  }

  private static int peakIntegerLiveness(MachineFunction function) {
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    int peak = 0;
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        int before = (int) liveness.liveBefore(instruction).stream()
            .filter(register -> !register.getType().isFloat()).count();
        int after = (int) liveness.liveAfter(instruction).stream()
            .filter(register -> !register.getType().isFloat()).count();
        peak = Math.max(peak, Math.max(before, after));
      }
    }
    return peak;
  }

  private static boolean hasCallsOutsideEntry(
      MachineFunction function, MachineBasicBlock entry) {
    return function.getBlocks().stream()
        .filter(block -> block != entry)
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == MachineOpcode.CALL);
  }
}
