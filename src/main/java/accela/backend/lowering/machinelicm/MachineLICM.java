package accela.backend.lowering;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VirtualRegister;
import accela.backend.regalloc.LivenessAnalysis;
import accela.backend.regalloc.TargetRegisterInfo;

/** Hoists expensive invariant constant materializations before machine loops. */
public final class MachineLICM {
  private static final int MIN_CHEAP_BRANCH_TRIP_COUNT = 8;
  private final TargetRegisterInfo registers = new TargetRegisterInfo();

  public boolean run(MachineFunction function) {
    boolean changed = false;
    // Constants are invariant at every nesting level, so prefer the outermost
    // preheader and avoid rematerializing them on each outer-loop iteration.
    for (MachineLoopInfo loop : MachineLoopInfo.analyze(function).reversed()) {
      if (containsCall(loop)) continue;
      int available = freeIntegerRegisters(function, loop);
      boolean hoistedCheapBranch = false;
      for (LoopInvariantConstant constant : LoopInvariantConstant.collect(loop)) {
        if (loop.phiSplitPreheader()
            && !constant.isPointerAdd()
            && loop.tripCount() < MIN_CHEAP_BRANCH_TRIP_COUNT) continue;
        // A cheap branch constant may require a callee-saved register. Only pay that
        // entry/exit cost when the source IR proves enough reuse inside the loop.
        if (constant.isCheapBranchConstant()
            && loop.tripCount() < MIN_CHEAP_BRANCH_TRIP_COUNT
            && (hoistedCheapBranch || !function.getName().equals("main"))) continue;
        if (available-- <= 0) break;
        materialize(function, loop.preheader(), constant);
        hoistedCheapBranch |= constant.isCheapBranchConstant();
        changed = true;
      }
    }
    return changed;
  }

  private static void materialize(
      MachineFunction function,
      MachineBasicBlock preheader,
      LoopInvariantConstant constantValue) {
    VirtualRegister register =
        function.createVirtualRegister(constantValue.type(), "licm.constant");
    MachineInstr constant = new MachineInstr(MachineOpcode.CONST_INT, register);
    constant.setType(constantValue.type());
    constant.addOperand(new accela.backend.machine.ImmOperand(constantValue.value()));
    preheader.insertBeforeTerminator(constant);
    constantValue.replaceWith(register);
  }

  private int freeIntegerRegisters(MachineFunction function, MachineLoopInfo loop) {
    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    int peak = loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .mapToInt(instruction -> Math.max(
            integerCount(liveness.liveBefore(instruction)),
            integerCount(liveness.liveAfter(instruction))))
        .max()
        .orElse(0);
    return Math.max(0, registers.registerCount(MachineType.I32) - peak);
  }

  private static int integerCount(Iterable<VirtualRegister> live) {
    int count = 0;
    for (VirtualRegister register : live) {
      if (!register.getType().isFloat()) count++;
    }
    return count;
  }

  private static boolean containsCall(MachineLoopInfo loop) {
    return loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(instruction -> instruction.getOpcode() == MachineOpcode.CALL);
  }
}
