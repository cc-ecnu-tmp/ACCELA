package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.List;

/** Merges multiple returns so frame lowering emits only one epilogue. */
public final class SharedReturnBlock {
  public boolean run(MachineFunction function) {
    List<MachineBasicBlock> returns = findReturnBlocks(function);
    if (returns.size() < 2) return false;

    MachineType returnType = function.getReturnType();
    VirtualRegister result = returnType == MachineType.VOID
        ? null : function.createVirtualRegister(returnType, "return");
    MachineBasicBlock exit = function.addBlock("shared.return");
    MachineInstr sharedReturn = new MachineInstr(MachineOpcode.RET, null);
    sharedReturn.setType(returnType);
    if (result != null) sharedReturn.addOperand(new VRegOperand(result));
    exit.addInstruction(sharedReturn);

    for (MachineBasicBlock block : returns) {
      List<MachineInstr> instructions = block.getInstructions();
      MachineInstr oldReturn = instructions.removeLast();
      if (result != null) {
        MachineInstr copy = new MachineInstr(MachineOpcode.MOVE, result);
        copy.setType(returnType);
        copy.addOperand(oldReturn.getOperands().getFirst());
        instructions.add(copy);
      }
      MachineInstr branch = new MachineInstr(MachineOpcode.BR, null);
      branch.addOperand(new BlockOperand(exit));
      instructions.add(branch);
    }
    return true;
  }

  private static List<MachineBasicBlock> findReturnBlocks(MachineFunction function) {
    List<MachineBasicBlock> returns = new ArrayList<>();
    for (MachineBasicBlock block : function.getBlocks()) {
      List<MachineInstr> instructions = block.getInstructions();
      if (!instructions.isEmpty()
          && instructions.getLast().getOpcode() == MachineOpcode.RET) {
        returns.add(block);
      }
    }
    return returns;
  }
}
