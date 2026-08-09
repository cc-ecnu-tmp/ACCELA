package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class PhiElimination {
  private int edgeSplitCounter = 0;

  public boolean run(MachineFunction function) {
    boolean changed = false;
    for (MachineBasicBlock block : new ArrayList<>(function.getBlocks())) {
      List<MachineInstr> phis = new ArrayList<>();
      for (MachineInstr instr : block.getInstructions()) {
        if (instr.getOpcode() == MachineOpcode.PHI) phis.add(instr);
      }
      if (phis.isEmpty()) continue;
      changed = true;

      Map<MachineBasicBlock, List<CopyOperation>> edgeCopies = new LinkedHashMap<>();
      for (MachineInstr phi : phis) {
        for (int i = 0; i < phi.getOperands().size(); i += 2) {
          MachineOperand src = phi.getOperands().get(i);
          MachineBasicBlock pred = ((BlockOperand) phi.getOperands().get(i + 1)).getBlock();
          edgeCopies.computeIfAbsent(pred, ignored -> new ArrayList<>()).add(new CopyOperation(phi.getDest(), src, phi.getType()));
        }
      }

      for (Map.Entry<MachineBasicBlock, List<CopyOperation>> entry : edgeCopies.entrySet()) {
        MachineBasicBlock pred = entry.getKey();
        MachineBasicBlock insertionBlock = ensureEdgeInsertionBlock(function, pred, block);
        emitSequentialCopies(function, insertionBlock, entry.getValue());
      }

      block.getInstructions().removeIf(instr -> instr.getOpcode() == MachineOpcode.PHI);
    }
    return changed;
  }

  private MachineBasicBlock ensureEdgeInsertionBlock(
      MachineFunction function, MachineBasicBlock pred, MachineBasicBlock succ) {
    // A self-edge PHI result is only live on the next iteration. Its copies may
    // execute speculatively before the loop exit without affecting that exit.
    if (pred == succ || successorCount(pred) <= 1) {
      return pred;
    }

    MachineBasicBlock split =
        function.addBlock(pred.getLabel() + ".to." + succ.getLabel() + ".phi." + edgeSplitCounter++);
    MachineInstr branch = new MachineInstr(MachineOpcode.BR, null);
    branch.addOperand(new BlockOperand(succ));
    split.addInstruction(branch);

    MachineInstr terminator = pred.getInstructions().get(pred.getInstructions().size() - 1);
    for (int i = 0; i < terminator.getOperands().size(); i++) {
      MachineOperand operand = terminator.getOperands().get(i);
      if (operand instanceof BlockOperand && ((BlockOperand) operand).getBlock() == succ) {
        terminator.setOperand(i, new BlockOperand(split));
      }
    }
    return split;
  }

  private int successorCount(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return 0;
    MachineInstr terminator = block.getInstructions().get(block.getInstructions().size() - 1);
    int count = 0;
    for (MachineOperand operand : terminator.getOperands()) {
      if (operand instanceof BlockOperand) count++;
    }
    return count;
  }

  private void emitSequentialCopies(
      MachineFunction function, MachineBasicBlock block, List<CopyOperation> copies) {
    List<CopyOperation> pending = new ArrayList<>();
    for (CopyOperation copy : copies) {
      if (copy.isIdentity()) continue;
      pending.add(copy);
    }

    while (!pending.isEmpty()) {
      boolean progressed = false;
      for (int i = 0; i < pending.size(); i++) {
        CopyOperation candidate = pending.get(i);
        if (isSafeToEmit(candidate, pending)) {
          insertMove(block, candidate.dest, candidate.src, candidate.type);
          pending.remove(i);
          progressed = true;
          break;
        }
      }

      if (progressed) continue;

      CopyOperation cycleCopy = pending.get(0);
      if (!(cycleCopy.src instanceof VRegOperand)) {
        insertMove(block, cycleCopy.dest, cycleCopy.src, cycleCopy.type);
        pending.remove(0);
        continue;
      }

      VirtualRegister temp = function.createVirtualRegister(cycleCopy.type, "phi.tmp");
      insertMove(block, temp, cycleCopy.src, cycleCopy.type);
      cycleCopy.src = new VRegOperand(temp);
    }
  }

  private boolean isSafeToEmit(CopyOperation candidate, List<CopyOperation> pending) {
    for (CopyOperation other : pending) {
      if (other == candidate) continue;
      if (other.readsRegister(candidate.dest)) return false;
    }
    return true;
  }

  private void insertMove(
      MachineBasicBlock block, VirtualRegister dest, MachineOperand src, MachineType type) {
    MachineInstr copy = new MachineInstr(MachineOpcode.MOVE, dest);
    copy.addOperand(src);
    copy.setType(type);
    block.insertBeforeTerminator(copy);
  }

  private static final class CopyOperation {
    private final VirtualRegister dest;
    private MachineOperand src;
    private final MachineType type;

    private CopyOperation(VirtualRegister dest, MachineOperand src, MachineType type) {
      this.dest = dest;
      this.src = src;
      this.type = type;
    }

    private boolean isIdentity() {
      return src instanceof VRegOperand && ((VRegOperand) src).getRegister().equals(dest);
    }

    private boolean readsRegister(VirtualRegister register) {
      return src instanceof VRegOperand && ((VRegOperand) src).getRegister().equals(register);
    }
  }
}
