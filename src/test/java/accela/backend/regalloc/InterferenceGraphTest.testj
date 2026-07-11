package accela.backend.regalloc;

import static org.junit.jupiter.api.Assertions.assertTrue;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineType;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import org.junit.jupiter.api.Test;

final class InterferenceGraphTest {
  @Test
  void deadDefinitionInterferesWithLiveThroughValue() {
    MachineFunction function = new MachineFunction("dead_def", MachineType.I32);
    MachineBasicBlock entry = function.addBlock("entry");
    VirtualRegister live = function.createVirtualRegister(MachineType.I32, "live");
    VirtualRegister dead = function.createVirtualRegister(MachineType.I32, "dead");
    VirtualRegister result = function.createVirtualRegister(MachineType.I32, "result");

    MachineInstr argument = new MachineInstr(MachineOpcode.ARG_IN, live);
    argument.setType(MachineType.I32);
    entry.addInstruction(argument);
    entry.addInstruction(add(dead, live));
    entry.addInstruction(add(result, live));

    MachineInstr ret = new MachineInstr(MachineOpcode.RET, null);
    ret.addOperand(new VRegOperand(result));
    ret.setType(MachineType.I32);
    entry.addInstruction(ret);

    LivenessAnalysis.Result liveness = LivenessAnalysis.analyze(function);
    InterferenceGraph graph = InterferenceGraph.build(function, liveness);

    assertTrue(graph.interferes(dead, live));
  }

  private static MachineInstr add(VirtualRegister destination, VirtualRegister source) {
    MachineInstr instruction = new MachineInstr(MachineOpcode.ADD, destination);
    instruction.addOperand(new VRegOperand(source));
    instruction.addOperand(new ImmOperand(1));
    instruction.setType(MachineType.I32);
    return instruction;
  }
}
