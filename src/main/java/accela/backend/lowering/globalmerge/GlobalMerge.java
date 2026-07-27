package accela.backend.lowering.globalmerge;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineType;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.backend.target.RISCVTarget;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Reuses indexed addresses of nearby globals and folds their displacement into memory offsets. */
public final class GlobalMerge {
  private final GlobalLayout layout;

  public GlobalMerge(MachineModule module, RISCVTarget target) {
    layout = new GlobalLayout(module.getSourceModule(), target);
  }

  public boolean run(MachineFunction function) {
    boolean changed = false;
    Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors = predecessors(function);
    Map<MachineBasicBlock, Map<Key, Address>> availableOut = new IdentityHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      List<MachineBasicBlock> incoming = predecessors.get(block);
      Map<Key, Address> available =
          incoming.size() == 1 && availableOut.containsKey(incoming.getFirst())
              ? new HashMap<>(availableOut.get(incoming.getFirst()))
              : new HashMap<>();
      for (MachineInstr instruction : List.copyOf(block.getInstructions())) {
        Address address = match(instruction);
        if (address == null) continue;
        Address existing = available.putIfAbsent(address.key(), address);
        if (existing == null
            || !MemoryOffsetRewriter.rewrite(
                function, address.register(), existing.register(),
                address.offset() - existing.offset())) continue;
        block.getInstructions().remove(instruction);
        changed = true;
      }
      availableOut.put(block, available);
    }
    return changed;
  }

  private Address match(MachineInstr instruction) {
    if (instruction.getOpcode() != MachineOpcode.ADD
        || instruction.getType() != MachineType.PTR) return null;
    SymbolOperand symbol = null;
    VRegOperand index = null;
    for (MachineOperand operand : instruction.getOperands()) {
      if (operand instanceof SymbolOperand value) symbol = value;
      else if (operand instanceof VRegOperand value) index = value;
      else return null;
    }
    if (symbol == null || index == null) return null;
    GlobalLayout.Location location = layout.locationOf(symbol.getSymbol());
    return location == null ? null : new Address(
        new Key(location.base(), index.getRegister()),
        instruction.getDest(), location.offset());
  }

  private static Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors(
      MachineFunction function) {
    Map<MachineBasicBlock, List<MachineBasicBlock>> result = new IdentityHashMap<>();
    function.getBlocks().forEach(block -> result.put(block, new ArrayList<>()));
    for (MachineBasicBlock block : function.getBlocks()) {
      if (block.getInstructions().isEmpty()) continue;
      for (MachineOperand operand : block.getInstructions().getLast().getOperands()) {
        if (operand instanceof BlockOperand successor)
          result.get(successor.getBlock()).add(block);
      }
    }
    return result;
  }

  private record Key(String base, VirtualRegister index) {}
  private record Address(Key key, VirtualRegister register, int offset) {}
}
