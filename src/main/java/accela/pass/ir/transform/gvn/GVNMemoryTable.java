package accela.pass.ir.transform.gvn;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.MemoryLocation;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Tracks available loads along one dominator-tree path. */
final class GVNMemoryTable {
  private final Map<MemoryKey, AvailableValue> values = new HashMap<>();

  GVNMemoryTable() {}

  private GVNMemoryTable(Map<MemoryKey, AvailableValue> values) {
    this.values.putAll(values);
  }

  GVNMemoryTable copy() {
    return new GVNMemoryTable(values);
  }

  void invalidateAll() {
    values.clear();
  }

  Value findOrAdd(Instruction instruction, GlobalModRefAnalysis.Result modRef) {
    if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
      MemoryLocation location = MemoryLocation.fromInstruction(instruction);
      AvailableValue existing = values.get(memoryKey(location));
      if (existing != null && hasUniquePath(existing.origin(), instruction)) {
        return existing.value();
      }
      values.put(memoryKey(location), new AvailableValue(location, instruction, instruction));
    } else if (instruction.getOpcode() == Instruction.Opcode.STORE) {
      MemoryLocation location = MemoryLocation.fromInstruction(instruction);
      values.values().removeIf(available -> mayOverlap(available.location(), location));
      values.put(
          memoryKey(location),
          new AvailableValue(location, instruction.getOperand(0), instruction));
    } else if (instruction.getOpcode() == Instruction.Opcode.CALL) {
      if (modRef == null) values.clear();
      else values.values().removeIf(
          available -> modRef.mayWrite(instruction, available.location().pointer()));
    }
    return null;
  }

  private static boolean mayOverlap(MemoryLocation left, MemoryLocation right) {
    return PointerProvenance.mayAlias(left.pointer(), right.pointer())
        && !left.isKnownDisjoint(right);
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

  private static MemoryKey memoryKey(MemoryLocation location) {
    return new MemoryKey(
        pointerKey(location.pointer()), location.accessType(), location.byteSize());
  }

  private static Object valueKey(Value value) {
    if (value instanceof Constant.Int integer) {
      return new IntegerKey(integer.getType().dataType, integer.value);
    }
    if (value instanceof Constant.Float floating) {
      return new FloatKey(Float.floatToRawIntBits(floating.value));
    }
    if (value instanceof Constant.Zero zero) {
      return new ZeroKey(zero.getType().toString());
    }
    if (value instanceof Constant.Vector vector) {
      return new VectorKey(
          vector.getType().toString(), vector.elements.stream().map(GVNMemoryTable::valueKey).toList());
    }
    return value;
  }

  private record AvailableValue(MemoryLocation location, Value value, Instruction origin) {}

  private record MemoryKey(Object pointer, Type accessType, long byteSize) {}

  private record MemoryExpression(
      Instruction.Opcode opcode, String type, String detail, List<Object> operands) {}

  private record IntegerKey(Type.DataType type, long value) {}

  private record FloatKey(int bits) {}

  private record ZeroKey(String type) {}

  private record VectorKey(String type, List<Object> elements) {}
}
