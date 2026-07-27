package accela.pass.ir.analysis;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/**
 * Conservatively proves that interleaving adjacent outer-loop iterations is memory-safe.
 */
public final class LoopNestDependenceAnalysis {
  private LoopNestDependenceAnalysis() {}

  public static boolean isSafe(
      Instruction outerInduction,
      long outerStep,
      Instruction innerInduction,
      List<BasicBlock> blocks,
      int factor) {
    List<MemoryAccess> accesses =
        collectAccesses(blocks, outerInduction, innerInduction);
    for (int left = 0; left < accesses.size(); left++) {
      for (int right = left; right < accesses.size(); right++) {
        MemoryAccess first = accesses.get(left);
        MemoryAccess second = accesses.get(right);
        if (!first.write() && !second.write()
            || !PointerProvenance.mayAlias(first.pointer(), second.pointer())) continue;
        if (!differentAcrossLanes(outerStep, first, second, factor)) return false;
      }
    }
    return true;
  }

  private static List<MemoryAccess> collectAccesses(
      List<BasicBlock> blocks, Value outerInduction, Value innerInduction) {
    List<MemoryAccess> accesses = new ArrayList<>();
    for (BasicBlock block : blocks) {
      for (Instruction instruction : block.getInstructions()) {
        if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
          accesses.add(MemoryAccess.create(
              instruction.getOperand(0), false, outerInduction, innerInduction));
        } else if (instruction.getOpcode() == Instruction.Opcode.STORE) {
          accesses.add(MemoryAccess.create(
              instruction.getOperand(1), true, outerInduction, innerInduction));
        }
      }
    }
    return accesses;
  }

  private static boolean differentAcrossLanes(
      long outerStep,
      MemoryAccess first,
      MemoryAccess second,
      int factor) {
    AddressFormula left = first.formula();
    AddressFormula right = second.formula();
    if (left == null || right == null || left.root != right.root) return false;

    for (int firstLane = 0; firstLane < factor; firstLane++) {
      for (int secondLane = 0; secondLane < factor; secondLane++) {
        if (firstLane == secondLane) continue;
        Long distance = left.distanceAtLanes(
            right, firstLane, secondLane, outerStep);
        if (distance == null || distance == 0) return false;
      }
    }
    return true;
  }

  private record MemoryAccess(Value pointer, boolean write, AddressFormula formula) {
    private static MemoryAccess create(
        Value pointer, boolean write, Value outerInduction, Value innerInduction) {
      return new MemoryAccess(
          pointer, write, AddressFormula.match(pointer, outerInduction, innerInduction));
    }
  }

  /** An inbounds GEP address represented by its typed array subscripts. */
  private static final class AddressFormula {
    private final Value root;
    private final Value outer;
    private final Value inner;
    private final List<AffineIndex> indices = new ArrayList<>();

    private AddressFormula(Value root, Value outer, Value inner) {
      this.root = root;
      this.outer = outer;
      this.inner = inner;
    }

    static AddressFormula match(Value pointer, Value outer, Value inner) {
      Value root = PointerProvenance.root(pointer);
      AddressFormula result = new AddressFormula(root, outer, inner);
      try {
        return result.addPointer(pointer) ? result : null;
      } catch (ArithmeticException overflow) {
        return null;
      }
    }

    private boolean addPointer(Value pointer) {
      if (pointer == root) return true;
      if (!(pointer instanceof Instruction gep)
          || gep.getOpcode() != Instruction.Opcode.GEP
          || !gep.isGepInbounds()
          || !addPointer(gep.getOperand(0))) return false;
      for (int index = 1; index < gep.getNumOperands(); index++) {
        AffineIndex affine = new AffineIndex(
            byteStride(gep.getGepSourceType(), index), outer, inner);
        affine.add(gep.getOperand(index), 1);
        indices.add(affine);
      }
      return true;
    }

    /**
     * Returns a non-zero subscript distance that proves the two typed objects are distinct.
     */
    private Long distanceAtLanes(
        AddressFormula other, int thisLane, int otherLane, long outerStep) {
      if (indices.size() != other.indices.size()) return null;
      for (int index = 0; index < indices.size(); index++) {
        AffineIndex left = indices.get(index);
        AffineIndex right = other.indices.get(index);
        if (left.byteStride != right.byteStride) return null;
        Long distance =
            left.distanceAtLanes(right, thisLane, otherLane, outerStep);
        if (distance != null && distance != 0) return distance;
      }
      return null;
    }
  }

  private static final class AffineIndex {
    private final long byteStride;
    private final Value outer;
    private final Value inner;
    private final Map<Value, Long> terms = new IdentityHashMap<>();
    private long offset;

    private AffineIndex(long byteStride, Value outer, Value inner) {
      this.byteStride = byteStride;
      this.outer = outer;
      this.inner = inner;
    }

    private void add(Value value, long scale) {
      if (value instanceof Constant.Int constant) {
        offset = Math.addExact(offset, Math.multiplyExact(scale, constant.value));
        return;
      }
      if (value instanceof Instruction instruction) {
        switch (instruction.getOpcode()) {
          case SEXT, ZEXT -> {
            add(instruction.getOperand(0), scale);
            return;
          }
          case ADD -> {
            add(instruction.getOperand(0), scale);
            add(instruction.getOperand(1), scale);
            return;
          }
          case SUB -> {
            add(instruction.getOperand(0), scale);
            add(instruction.getOperand(1), Math.negateExact(scale));
            return;
          }
          case MUL -> {
            if (addProduct(instruction, scale)) return;
          }
          default -> {}
        }
      }
      long coefficient = Math.addExact(terms.getOrDefault(value, 0L), scale);
      if (coefficient == 0) terms.remove(value);
      else terms.put(value, coefficient);
    }

    private boolean addProduct(Instruction multiply, long scale) {
      for (int constantIndex = 0; constantIndex < 2; constantIndex++) {
        if (multiply.getOperand(constantIndex) instanceof Constant.Int constant) {
          add(multiply.getOperand(1 - constantIndex),
              Math.multiplyExact(scale, constant.value));
          return true;
        }
      }
      return false;
    }

    private Long distanceAtLanes(
        AffineIndex other, int thisLane, int otherLane, long outerStep) {
      if (terms.getOrDefault(inner, 0L) != 0
          || other.terms.getOrDefault(inner, 0L) != 0) return null;
      long thisOuter = terms.getOrDefault(outer, 0L);
      long otherOuter = other.terms.getOrDefault(outer, 0L);
      if (thisOuter != otherOuter) return null;

      for (var term : terms.entrySet()) {
        if (term.getKey() == outer || term.getKey() == inner) continue;
        if (!term.getValue().equals(other.terms.get(term.getKey()))) return null;
      }
      for (var term : other.terms.entrySet()) {
        if (term.getKey() == outer || term.getKey() == inner) continue;
        if (!term.getValue().equals(terms.get(term.getKey()))) return null;
      }
      try {
        long laneDistance = Math.multiplyExact(
            thisOuter, Math.multiplyExact((long) thisLane - otherLane, outerStep));
        return Math.multiplyExact(
            Math.addExact(Math.subtractExact(offset, other.offset), laneDistance),
            byteStride);
      } catch (ArithmeticException overflow) {
        return null;
      }
    }
  }

  private static long byteStride(Type sourceType, int operandIndex) {
    Type type = sourceType;
    for (int index = 1; index < operandIndex; index++) {
      if (type.isArray()) type = type.innerType;
    }
    return sizeOf(type);
  }

  private static long sizeOf(Type type) {
    if (type.isArray()) return Math.multiplyExact(type.size, sizeOf(type.innerType));
    if (type == Type.I64 || type.isPointer()) return 8;
    return type == Type.I1 ? 1 : 4;
  }
}
