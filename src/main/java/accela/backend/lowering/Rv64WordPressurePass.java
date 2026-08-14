package accela.backend.lowering;

import accela.backend.BackendPipeline;
import accela.backend.instrument.BackendPassInstrumentation;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import accela.pass.PassDescriptor;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import java.util.IdentityHashMap;
import java.util.Map;

/** Safe local machine rewrite for redundant same-width sign extensions. */
public final class Rv64WordPressurePass implements BackendPipeline.CandidateFunctionPass {
  private final BackendPassInstrumentation instrumentation;
  private final PassDescriptor descriptor;
  private final int occurrence;

  public Rv64WordPressurePass(
      BackendPassInstrumentation instrumentation,
      PassDescriptor descriptor,
      int occurrence) {
    this.instrumentation = instrumentation;
    this.descriptor = descriptor;
    this.occurrence = occurrence;
  }

  @Override
  public boolean run(MachineFunction function) {
    PassDecisionEmitter decision = instrumentation.decisionEmitter(
        descriptor, occurrence, "machine_function", "@" + function.getName());
    decision.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
    boolean changed = false;
    for (MachineBasicBlock block : function.getBlocks()) {
      Map<VirtualRegister, MachineOperand> replacements = new IdentityHashMap<>();
      for (var iterator = block.getInstructions().iterator(); iterator.hasNext();) {
        MachineInstr instruction = iterator.next();
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          instruction.setOperand(index, resolve(instruction.getOperands().get(index), replacements));
        }
        if (instruction.getDest() != null && isRematerializable(instruction)) {
          function.markRematerializable(instruction.getDest(), instruction);
        }
        if (instruction.getOpcode() != MachineOpcode.SEXT
            || instruction.getDest() == null
            || instruction.getOperands().size() != 1
            || !(instruction.getOperands().getFirst() instanceof VRegOperand source)
            || !function.isKnownSext32(source.getRegister())
            || instruction.getDest().getType() != source.getRegister().getType()) {
          if (instruction.getOpcode() == MachineOpcode.SEXT
              && instruction.getDest() != null
              && instruction.getOperands().size() == 1
              && instruction.getOperands().getFirst() instanceof VRegOperand source
              && source.getRegister().getType() == accela.backend.machine.MachineType.I32
              && instruction.getDest().getType() == accela.backend.machine.MachineType.I64) {
            function.markKnownSext32(instruction.getDest());
          }
          continue;
        }
        replacements.put(instruction.getDest(), new VRegOperand(source.getRegister()));
        iterator.remove();
        changed = true;
      }
    }
    if (changed) {
      decision.applied(DecisionReasonCode.APPLIED_CANONICALIZATION);
    } else {
      decision.rejected(DecisionReasonCode.REJECTED_NO_BENEFIT);
    }
    return changed;
  }

  private static boolean isRematerializable(MachineInstr instruction) {
    if (instruction.getOpcode() == MachineOpcode.CONST_INT
        && instruction.getOperands().size() == 1
        && instruction.getOperands().getFirst() instanceof ImmOperand) return true;
    if (instruction.getOpcode() == MachineOpcode.MOVE
        && instruction.getOperands().size() == 1
        && instruction.getOperands().getFirst() instanceof SymbolOperand) return true;
    return instruction.getOpcode() == MachineOpcode.ADD
        && instruction.getOperands().size() == 2
        && instruction.getOperands().get(0) instanceof SymbolOperand
        && instruction.getOperands().get(1) instanceof ImmOperand immediate
        && immediate.getValue() >= -2048 && immediate.getValue() <= 2047;
  }

  private static MachineOperand resolve(
      MachineOperand operand, Map<VirtualRegister, MachineOperand> replacements) {
    while (operand instanceof VRegOperand register) {
      MachineOperand replacement = replacements.get(register.getRegister());
      if (replacement == null || replacement == operand) return operand;
      operand = replacement;
    }
    return operand;
  }
}
