package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineOpcode;
import accela.backend.machine.MachineOperand;
import accela.backend.machine.SymbolOperand;
import accela.backend.machine.VRegOperand;
import accela.backend.machine.VirtualRegister;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Eliminates repeated side-effect-free machine expressions within a basic block. */
public final class MachineCSE {
  public boolean run(MachineFunction function) {
    boolean changed = false;
    Map<MachineBasicBlock, List<MachineBasicBlock>> predecessors = predecessors(function);
    Map<MachineBasicBlock, Map<Expression, VirtualRegister>> availableOut = new IdentityHashMap<>();
    for (var block : function.getBlocks()) {
      List<MachineBasicBlock> incoming = predecessors.get(block);
      Map<Expression, VirtualRegister> available =
          incoming.size() == 1 && availableOut.containsKey(incoming.getFirst())
              ? new HashMap<>(availableOut.get(incoming.getFirst()))
              : new HashMap<>();
      for (MachineInstr instruction : List.copyOf(block.getInstructions())) {
        if (!isPure(instruction)) continue;
        Expression expression = expressionFor(instruction);
        VirtualRegister existing = available.putIfAbsent(expression, instruction.getDest());
        if (existing == null) continue;
        replaceUses(function, instruction.getDest(), existing);
        block.getInstructions().remove(instruction);
        changed = true;
      }
      availableOut.put(block, available);
    }
    return changed;
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

  private static boolean isPure(MachineInstr instruction) {
    if (instruction.getDest() == null) return false;
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SHL, ASHR, AND, XOR, ZEXT, SEXT -> true;
      default -> false;
    };
  }

  private static Expression expressionFor(MachineInstr instruction) {
    List<Object> operands =
        instruction.getOperands().stream().map(MachineCSE::operandKey).toList();
    return new Expression(instruction.getOpcode(), instruction.getType(), operands);
  }

  private static Object operandKey(MachineOperand operand) {
    if (operand instanceof VRegOperand register) return register.getRegister();
    if (operand instanceof ImmOperand immediate) return new Immediate(immediate.getValue());
    if (operand instanceof SymbolOperand symbol) return new Symbol(symbol.getSymbol());
    return operand;
  }

  private static void replaceUses(
      MachineFunction function, VirtualRegister removed, VirtualRegister replacement) {
    for (var block : function.getBlocks()) {
      for (MachineInstr instruction : block.getInstructions()) {
        for (int index = 0; index < instruction.getOperands().size(); index++) {
          if (instruction.getOperands().get(index) instanceof VRegOperand register
              && register.getRegister().equals(removed)) {
            instruction.setOperand(index, new VRegOperand(replacement));
          }
        }
      }
    }
  }
  private record Expression(
      MachineOpcode opcode, accela.backend.machine.MachineType type, List<Object> operands) {}
  private record Immediate(long value) {}
  private record Symbol(String name) {}
}
