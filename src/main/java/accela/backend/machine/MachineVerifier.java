package accela.backend.machine;

import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

/** Structural verifier for pre-allocation Machine IR snapshots. */
public final class MachineVerifier {
  private MachineVerifier() {}

  public static void verify(MachineModule module) {
    Set<MachineFunction> functions = identitySet();
    for (MachineFunction function : module.getFunctions()) {
      if (!functions.add(function)) fail("duplicate machine function object");
      verify(function);
    }
  }

  public static void verify(MachineFunction function) {
    Set<MachineBasicBlock> blocks = identitySet();
    Set<VirtualRegister> known = identitySet();
    known.addAll(function.getArguments());
    if (function.getBlocks().isEmpty()) fail(function.getName() + ": no basic blocks");
    for (MachineBasicBlock block : function.getBlocks()) {
      if (!blocks.add(block)) fail(function.getName() + ": duplicate basic block object");
      if (block.getInstructions().isEmpty()) fail(function.getName() + ": empty basic block");
      for (int index = 0; index < block.getInstructions().size(); index++) {
        MachineInstr instruction = block.getInstructions().get(index);
        if (isTerminator(instruction.getOpcode()) && index != block.getInstructions().size() - 1) {
          fail(function.getName() + ": terminator is not last in block " + block.getLabel());
        }
        if (instruction.getDest() != null) {
          known.add(instruction.getDest());
        }
      }
      if (!isTerminator(block.getInstructions().getLast().getOpcode())) {
        fail(function.getName() + ": block has no terminator: " + block.getLabel());
      }
    }
    for (MachineBasicBlock block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (MachineOperand operand : instruction.getOperands()) {
          if (operand instanceof VRegOperand register && !known.contains(register.getRegister())) {
            fail(function.getName() + ": operand references an unknown virtual register");
          }
          if (operand instanceof BlockOperand target && !blocks.contains(target.getBlock())) {
            fail(function.getName() + ": branch references a foreign basic block");
          }
        }
      }
    }
  }

  private static <T> Set<T> identitySet() {
    return Collections.newSetFromMap(new IdentityHashMap<>());
  }

  private static boolean isTerminator(MachineOpcode opcode) {
    return opcode == MachineOpcode.BR || opcode == MachineOpcode.CONDBR
        || opcode == MachineOpcode.RET;
  }

  private static void fail(String message) {
    throw new IllegalStateException("invalid Machine IR: " + message);
  }
}
