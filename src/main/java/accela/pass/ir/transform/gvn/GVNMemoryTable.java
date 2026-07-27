package accela.pass.ir.transform.gvn;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Tracks available loads along one dominator-tree path. */
final class GVNMemoryTable {
  private final Map<Object, AvailableLoad> loads = new HashMap<>();

  GVNMemoryTable() {}

  private GVNMemoryTable(Map<Object, AvailableLoad> loads) {
    this.loads.putAll(loads);
  }

  GVNMemoryTable copy() {
    return new GVNMemoryTable(loads);
  }

  Value findOrAdd(Instruction instruction, GlobalModRefAnalysis.Result modRef) {
    if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
      Value pointer = instruction.getOperand(0);
      AvailableLoad existing = loads.get(pointerKey(pointer));
      if (existing != null && hasUniquePath(existing.value(), instruction)) {
        return existing.value();
      }
      loads.put(pointerKey(pointer), new AvailableLoad(pointer, instruction));
    } else if (instruction.getOpcode() == Instruction.Opcode.STORE) {
      Value pointer = instruction.getOperand(1);
      loads.values().removeIf(
          load -> PointerProvenance.mayAlias(load.pointer(), pointer));
    } else if (instruction.getOpcode() == Instruction.Opcode.CALL) {
      if (modRef == null) loads.clear();
      else loads.values().removeIf(
          load -> modRef.mayWrite(instruction, load.pointer()));
    }
    return null;
  }

  /**
   * Restrict forwarding to a straight CFG path. This prevents a dominating load from crossing
   * a loop backedge that may write its location.
   */
  private static boolean hasUniquePath(Instruction from, Instruction to) {
    BasicBlock block = to.getParent();
    while (block != from.getParent()) {
      List<BasicBlock> predecessors = block.getPredecessors();
      if (predecessors.size() != 1 || predecessors.getFirst() == block) return false;
      block = predecessors.getFirst();
    }
    return true;
  }

  private static Object pointerKey(Value value) {
    if (!(value instanceof Instruction instruction)
        || (instruction.getOpcode() != Instruction.Opcode.GEP
            && instruction.getOpcode() != Instruction.Opcode.SEXT
            && instruction.getOpcode() != Instruction.Opcode.ZEXT)) return valueKey(value);
    List<Object> operands = new ArrayList<>();
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      operands.add(pointerKey(instruction.getOperand(index)));
    }
    String detail = instruction.getOpcode() == Instruction.Opcode.GEP
        ? instruction.getGepSourceType() + ":" + instruction.isGepInbounds() : "";
    return new MemoryExpression(
        instruction.getOpcode(), instruction.getType().toString(), detail, List.copyOf(operands));
  }

  private static Object valueKey(Value value) {
    if (value instanceof Constant.Int integer) {
      return new IntegerKey(integer.getType().dataType, integer.value);
    }
    if (value instanceof Constant.Float floating) {
      return new FloatKey(Float.floatToRawIntBits(floating.value));
    }
    return value;
  }

  private record AvailableLoad(Value pointer, Instruction value) {}

  private record MemoryExpression(
      Instruction.Opcode opcode, String type, String detail, List<Object> operands) {}

  private record IntegerKey(Type.DataType type, long value) {}

  private record FloatKey(int bits) {}
}
