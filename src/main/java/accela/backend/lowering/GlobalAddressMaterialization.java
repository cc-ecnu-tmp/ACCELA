package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
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
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Hoists frequently referenced global addresses into allocator-managed registers. */
public final class GlobalAddressMaterialization {
  private static final int MIN_USES = 3;
  private final TargetRegisterInfo registers = new TargetRegisterInfo();

  public boolean run(MachineFunction function) {
    MachineBasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    Map<String, Integer> weightedUses = new LinkedHashMap<>();
    Map<String, Integer> actualUses = new LinkedHashMap<>();
    Set<String> nonEntryUses = new LinkedHashSet<>();
    Set<MachineBasicBlock> loopBlocks = findLoopBlocks(function);
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (var operand : instruction.getOperands()) {
          if (!(operand instanceof SymbolOperand symbol)) continue;
          // A single reference in a loop is more profitable than several cold references:
          // hoisting `la` removes it from every dynamic iteration.
          weightedUses.merge(
              symbol.getSymbol(), loopBlocks.contains(block) ? MIN_USES : 1, Integer::sum);
          actualUses.merge(symbol.getSymbol(), 1, Integer::sum);
          if (block != entry) nonEntryUses.add(symbol.getSymbol());
        }
      }
    }

    boolean recursive = hasSelfCall(function);
    Set<String> liveAcrossCalls = symbolsLiveAcrossCalls(function);
    int available = availableRegisters(function);
    Map<String, VirtualRegister> addresses = new LinkedHashMap<>();
    weightedUses.entrySet().stream()
        .filter(symbol -> symbol.getValue() >= MIN_USES)
        .filter(symbol -> nonEntryUses.contains(symbol.getKey()))
        // Loop depth alone cannot pay for saving another callee-saved register
        // in every invocation. Require repeated static uses across calls.
        .filter(symbol -> !recursive
            || !liveAcrossCalls.contains(symbol.getKey())
            || actualUses.get(symbol.getKey()) >= MIN_USES)
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
      entry.getInstructions().add(0, materialize);
    }
    return true;
  }

  private int availableRegisters(MachineFunction function) {
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    int peak = 0;
    int acrossCalls = 0;
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        int before = (int) liveness.liveBefore(instruction).stream()
            .filter(register -> !register.getType().isFloat()).count();
        int after = (int) liveness.liveAfter(instruction).stream()
            .filter(register -> !register.getType().isFloat()).count();
        peak = Math.max(peak, Math.max(before, after));
        if (instruction.getOpcode() == MachineOpcode.CALL) {
          acrossCalls = Math.max(
              acrossCalls,
              (int) liveness.liveBefore(instruction).stream()
                  .filter(liveness.liveAfter(instruction)::contains)
                  .filter(register -> !register.getType().isFloat()).count());
        }
      }
    }
    int available = registers.registerCount(MachineType.PTR) - peak;
    if (acrossCalls > 0) {
      available = Math.min(
          available,
          registers.calleeSavedRegisters(MachineType.PTR).size() - acrossCalls);
    }
    return Math.max(0, available);
  }

  private static Set<MachineBasicBlock> findLoopBlocks(MachineFunction function) {
    Set<MachineBasicBlock> loops = new HashSet<>();
    for (MachineBasicBlock start : function.getBlocks()) {
      Set<MachineBasicBlock> visited = new HashSet<>();
      ArrayDeque<MachineBasicBlock> worklist = new ArrayDeque<>(successors(start));
      while (!worklist.isEmpty() && !loops.contains(start)) {
        MachineBasicBlock block = worklist.removeFirst();
        if (block == start) {
          loops.add(start);
        } else if (visited.add(block)) {
          worklist.addAll(successors(block));
        }
      }
    }
    return loops;
  }

  private static Set<String> symbolsLiveAcrossCalls(MachineFunction function) {
    Map<MachineBasicBlock, Set<String>> uses = new IdentityHashMap<>();
    Map<MachineBasicBlock, Set<String>> liveIn = new IdentityHashMap<>();
    Map<MachineBasicBlock, Set<String>> liveOut = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      uses.put(block, symbolUses(block));
      liveIn.put(block, new LinkedHashSet<>());
      liveOut.put(block, new LinkedHashSet<>());
    }

    boolean changed;
    do {
      changed = false;
      for (MachineBasicBlock block : function.getBlocks().reversed()) {
        Set<String> out = new LinkedHashSet<>();
        for (MachineBasicBlock successor : successors(block)) {
          out.addAll(liveIn.get(successor));
        }
        Set<String> in = new LinkedHashSet<>(out);
        in.addAll(uses.get(block));
        if (!out.equals(liveOut.get(block)) || !in.equals(liveIn.get(block))) {
          liveOut.put(block, out);
          liveIn.put(block, in);
          changed = true;
        }
      }
    } while (changed);

    Set<String> result = new LinkedHashSet<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      Set<String> live = new LinkedHashSet<>(liveOut.get(block));
      List<MachineInstr> instructions = new ArrayList<>(block.getInstructions());
      for (MachineInstr instruction : instructions.reversed()) {
        if (instruction.getOpcode() == MachineOpcode.CALL) result.addAll(live);
        addSymbolUses(instruction, live);
      }
    }
    return result;
  }

  private static boolean hasSelfCall(MachineFunction function) {
    return function.getBlocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == MachineOpcode.CALL
            && function.getName().equals(instruction.getCallee()));
  }

  private static Set<String> symbolUses(MachineBasicBlock block) {
    Set<String> result = new LinkedHashSet<>();
    for (MachineInstr instruction : block.getInstructions()) {
      addSymbolUses(instruction, result);
    }
    return result;
  }

  private static void addSymbolUses(MachineInstr instruction, Set<String> symbols) {
    for (var operand : instruction.getOperands()) {
      if (operand instanceof SymbolOperand symbol) symbols.add(symbol.getSymbol());
    }
  }

  private static Set<MachineBasicBlock> successors(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return Set.of();
    Set<MachineBasicBlock> successors = new LinkedHashSet<>();
    for (var operand : block.getInstructions().getLast().getOperands()) {
      if (operand instanceof BlockOperand target) successors.add(target.getBlock());
    }
    return successors;
  }
}
