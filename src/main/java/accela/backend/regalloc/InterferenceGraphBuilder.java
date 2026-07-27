package accela.backend.regalloc;

import java.util.HashSet;
import java.util.Set;

import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;

public final class InterferenceGraphBuilder {
    public record Move(
        VirtualRegister dst, VirtualRegister src, MachineInstr instr, int order) {}

    public record Result(InterferenceGraph graph, Set<Move> moves) {}
 
    public static Result build(MachineFunction function, LivenessAnalysis.Result res){
        Set<Move> moves = new HashSet<>();
        InterferenceGraph graph = new InterferenceGraph();
        int instructionOrder = 0;
        for (MachineBasicBlock blc : function.getBlocks()){
            addSimultaneouslyLiveEdges(graph, res.liveIn(blc));
            for(MachineInstr instr : blc.getInstructions()){

                for(VirtualRegister register : getInstrUse(instr)){
                    graph.addNode(register);
                }

                var def = instr.getDest();
                if(def == null) continue;
                
                graph.addNode(def);
                
                var moveSrc = getRegisterMoveSource(instr);
                if (moveSrc != null) {
                    moves.add(new Move(def, moveSrc, instr, instructionOrder));
                }

                for (VirtualRegister register : res.liveAfter(instr)) {
                    if (register.equals(moveSrc) || !sharesRegisterClass(def, register)) {
                        continue;
                    }
                    graph.addEdge(def, register);
                }
                instructionOrder++;
            }
        }

        return new Result(graph, moves);
    }

    private static void addSimultaneouslyLiveEdges(
            InterferenceGraph graph, Set<VirtualRegister> liveRegisters) {
        VirtualRegister[] registers = liveRegisters.toArray(VirtualRegister[]::new);
        for (int i = 0; i < registers.length; i++) {
            graph.addNode(registers[i]);
            for (int j = i + 1; j < registers.length; j++) {
                if (sharesRegisterClass(registers[i], registers[j])) {
                    graph.addEdge(registers[i], registers[j]);
                }
            }
        }
    }

    private static VirtualRegister getRegisterMoveSource(MachineInstr instr) {
        if (instr.getOpcode() != MachineOpcode.MOVE || !instr.isCoalescable()) {
        return null;
        }

        if (instr.getOperands().isEmpty()) {
        return null;
        }

        MachineOperand operand = instr.getOperands().get(0);
        if (!(operand instanceof VRegOperand)) {
        return null;
        }

        return ((VRegOperand) operand).getRegister();
    }

    private static boolean sharesRegisterClass(VirtualRegister left, VirtualRegister right) {
        if (left.getType().isFloat()) {
            return right.getType().isFloat();
        }
        if (left.getType().isIntegerLike()) {
            return right.getType().isIntegerLike();
        }
        return false;
    }

    private static Set<VirtualRegister> getInstrUse(MachineInstr instr) {
      Set<VirtualRegister> result = new HashSet<>();

      for (MachineOperand operand : instr.getOperands()) {
        if (operand instanceof VRegOperand) {
          result.add(((VRegOperand) operand).getRegister());
        }
      }

      return result;
    }
}
