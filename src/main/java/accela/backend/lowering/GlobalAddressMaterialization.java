package accela.backend.lowering;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.LinkedHashMap;
import java.util.Map;

/** Materializes frequently used global addresses once per function. */
public final class GlobalAddressMaterialization {
  private static final int MIN_USES = 3;

  public boolean run(MachineFunction function) {
    MachineBasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    Map<String, Integer> counts = new LinkedHashMap<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (var operand : instruction.getOperands()) {
          if (operand instanceof SymbolOperand symbol) {
            counts.merge(symbol.getSymbol(), 1, Integer::sum);
          }
        }
      }
    }

    Map<String, VirtualRegister> addresses = new LinkedHashMap<>();
    for (var symbol : counts.entrySet()) {
      if (symbol.getValue() < MIN_USES) continue;
      addresses.put(symbol.getKey(),
          function.createVirtualRegister(MachineType.PTR, "global.addr"));
    }
    if (addresses.isEmpty()) return false;

    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (instruction.getOperands().get(index) instanceof SymbolOperand symbol) {
            VirtualRegister address = addresses.get(symbol.getSymbol());
            if (address != null) instruction.setOperand(index, new VRegOperand(address));
          }
        }
      }
    }

    int insertAt = 0;
    for (var address : addresses.entrySet()) {
      MachineInstr materialize = new MachineInstr(MachineOpcode.MOVE, address.getValue());
      materialize.setType(MachineType.PTR);
      materialize.addOperand(new SymbolOperand(address.getKey()));
      entry.getInstructions().add(insertAt++, materialize);
    }
    return true;
  }
}
