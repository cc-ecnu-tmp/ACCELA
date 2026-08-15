package accela.pass.ir.analysis.dependence;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.ir.analysis.MemoryLocation;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** An inbounds GEP chain represented as affine typed array subscripts. */
final class AffineAccess {
  enum Relation {
    DEPENDENT,
    DISJOINT,
    UNKNOWN
  }

  record Distance(Relation relation, List<Long> distances) {}

  private final Value root;
  private final List<Value> inductions;
  private final List<AffineIndex> indices = new ArrayList<>();

  private AffineAccess(Value root, List<Value> inductions) {
    this.root = root;
    this.inductions = inductions;
  }

  static AffineAccess match(Value pointer, List<Value> inductions) {
    AffineAccess result =
        new AffineAccess(PointerProvenance.root(pointer), List.copyOf(inductions));
    try {
      return result.addPointer(pointer) ? result : null;
    } catch (ArithmeticException overflow) {
      return null;
    }
  }

  Value root() {
    return root;
  }

  Long byteStride(int loop) {
    long result = 0;
    try {
      for (AffineIndex index : indices) {
        if (!index.analyzable) return null;
        result = Math.addExact(
            result,
            Math.multiplyExact(index.byteStride, index.coefficient(inductions.get(loop))));
      }
      return result;
    } catch (ArithmeticException overflow) {
      return null;
    }
  }

  /** Whether typed-index disjointness is sufficient for an access of this width. */
  boolean supportsAccessSize(long byteSize) {
    long minimumStride = Long.MAX_VALUE;
    for (AffineIndex index : indices) {
      minimumStride = Math.min(minimumStride, index.byteStride);
    }
    return minimumStride == Long.MAX_VALUE || byteSize <= minimumStride;
  }

  Distance distanceTo(AffineAccess other) {
    if (indices.size() != other.indices.size()) {
      return new Distance(Relation.UNKNOWN, List.of());
    }
    List<Long> distances = new ArrayList<>();
    for (int loop = 0; loop < inductions.size(); loop++) distances.add(null);
    boolean unknown = false;
    for (int index = 0; index < indices.size(); index++) {
      IndexDistance constraint =
          indices.get(index).distanceTo(other.indices.get(index), inductions);
      if (constraint.relation() == Relation.DISJOINT) {
        return new Distance(Relation.DISJOINT, List.of());
      }
      if (constraint.relation() == Relation.UNKNOWN) {
        unknown = true;
        continue;
      }
      if (constraint.loop() < 0) continue;
      Long previous = distances.get(constraint.loop());
      if (previous != null && previous.longValue() != constraint.distance()) {
        return new Distance(Relation.DISJOINT, List.of());
      }
      distances.set(constraint.loop(), constraint.distance());
    }
    return new Distance(unknown ? Relation.UNKNOWN : Relation.DEPENDENT, distances);
  }

  boolean provesDistinctAtLanes(
      AffineAccess other,
      int variedLoop,
      int independentLoop,
      int thisLane,
      int otherLane,
      long step,
      long thisAccessSize,
      long otherAccessSize) {
    if (indices.size() != other.indices.size()) return false;
    try {
      long byteDistance = 0;
      for (int index = 0; index < indices.size(); index++) {
        Long distance = indices.get(index).distanceAtLanes(
            other.indices.get(index),
            inductions,
            variedLoop,
            independentLoop,
            thisLane,
            otherLane,
            step);
        if (distance == null) return false;
        byteDistance = Math.addExact(byteDistance, distance);
      }
      return MemoryLocation.areDisjointAtOffset(
          byteDistance, thisAccessSize, otherAccessSize);
    } catch (ArithmeticException overflow) {
      return false;
    }
  }

  private boolean addPointer(Value pointer) {
    if (pointer == root) return true;
    if (!(pointer instanceof Instruction gep)
        || gep.getOpcode() != Instruction.Opcode.GEP
        || !gep.isGepInbounds()
        || !addPointer(gep.getOperand(0))) return false;
    for (int operand = 1; operand < gep.getNumOperands(); operand++) {
      AffineIndex index =
          new AffineIndex(byteStride(gep.getGepSourceType(), operand), inductions);
      index.add(gep.getOperand(operand), 1);
      indices.add(index);
    }
    return true;
  }

  private record IndexDistance(Relation relation, int loop, long distance) {
    static IndexDistance dependent() {
      return new IndexDistance(Relation.DEPENDENT, -1, 0);
    }

    static IndexDistance dependent(int loop, long distance) {
      return new IndexDistance(Relation.DEPENDENT, loop, distance);
    }

    static IndexDistance disjoint() {
      return new IndexDistance(Relation.DISJOINT, -1, 0);
    }

    static IndexDistance unknown() {
      return new IndexDistance(Relation.UNKNOWN, -1, 0);
    }
  }

  private static final class AffineIndex {
    private final long byteStride;
    private final List<Value> inductions;
    private final Map<Value, Long> terms = new IdentityHashMap<>();
    private long offset;
    private boolean analyzable = true;

    private AffineIndex(long byteStride, List<Value> inductions) {
      this.byteStride = byteStride;
      this.inductions = inductions;
    }

    private void add(Value value, long scale) {
      if (value instanceof Constant.Int constant) {
        offset = Math.addExact(offset, Math.multiplyExact(scale, constant.value));
        return;
      }
      if (inductions.contains(value)) {
        addTerm(value, scale);
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
      if (dependsOnInduction(value, new IdentityHashMap<>())) analyzable = false;
      addTerm(value, scale);
    }

    private void addTerm(Value value, long scale) {
      long coefficient = Math.addExact(terms.getOrDefault(value, 0L), scale);
      if (coefficient == 0) terms.remove(value);
      else terms.put(value, coefficient);
    }

    private boolean addProduct(Instruction multiply, long scale) {
      for (int constantIndex = 0; constantIndex < 2; constantIndex++) {
        if (multiply.getOperand(constantIndex) instanceof Constant.Int constant) {
          add(
              multiply.getOperand(1 - constantIndex),
              Math.multiplyExact(scale, constant.value));
          return true;
        }
      }
      return false;
    }

    private boolean dependsOnInduction(Value value, Map<Value, Boolean> visited) {
      if (inductions.contains(value)) return true;
      if (!(value instanceof Instruction instruction) || visited.put(value, true) != null) {
        return false;
      }
      for (int operand = 0; operand < instruction.getNumOperands(); operand++) {
        if (dependsOnInduction(instruction.getOperand(operand), visited)) return true;
      }
      return false;
    }

    private long coefficient(Value value) {
      return terms.getOrDefault(value, 0L);
    }

    private IndexDistance distanceTo(AffineIndex other, List<Value> loops) {
      if (byteStride != other.byteStride || !analyzable || !other.analyzable) {
        return IndexDistance.unknown();
      }
      if (!sameInvariantTerms(other, loops)) return IndexDistance.unknown();

      try {
        int varyingLoop = -1;
        long coefficient = 0;
        for (int loop = 0; loop < loops.size(); loop++) {
          long left = coefficient(loops.get(loop));
          if (left != other.coefficient(loops.get(loop))) return IndexDistance.unknown();
          if (left == 0) continue;
          if (varyingLoop >= 0) return IndexDistance.unknown();
          varyingLoop = loop;
          coefficient = left;
        }
        long delta = Math.subtractExact(offset, other.offset);
        if (varyingLoop < 0) {
          return delta == 0 ? IndexDistance.dependent() : IndexDistance.disjoint();
        }
        if (delta == Long.MIN_VALUE && coefficient == -1) {
          return IndexDistance.unknown();
        }
        if (delta % coefficient != 0) return IndexDistance.disjoint();
        return IndexDistance.dependent(varyingLoop, delta / coefficient);
      } catch (ArithmeticException overflow) {
        return IndexDistance.unknown();
      }
    }

    private Long distanceAtLanes(
        AffineIndex other,
        List<Value> loops,
        int variedLoop,
        int independentLoop,
        int thisLane,
        int otherLane,
        long step) {
      if (byteStride != other.byteStride
          || !analyzable
          || !other.analyzable
          || coefficient(loops.get(independentLoop))
              != other.coefficient(loops.get(independentLoop))
          || coefficient(loops.get(variedLoop))
              != other.coefficient(loops.get(variedLoop))
          || !sameTermsExcept(other, loops.get(variedLoop), loops.get(independentLoop))) {
        return null;
      }
      try {
        long laneDelta = Math.multiplyExact(
            coefficient(loops.get(variedLoop)),
            Math.multiplyExact((long) thisLane - otherLane, step));
        return Math.multiplyExact(
            Math.addExact(Math.subtractExact(offset, other.offset), laneDelta),
            byteStride);
      } catch (ArithmeticException overflow) {
        return null;
      }
    }

    private boolean sameInvariantTerms(AffineIndex other, List<Value> loops) {
      return sameTermsExcept(other, loops.toArray(Value[]::new));
    }

    private boolean sameTermsExcept(AffineIndex other, Value... ignored) {
      for (var term : terms.entrySet()) {
        if (containsIdentity(ignored, term.getKey())) continue;
        if (!term.getValue().equals(other.terms.get(term.getKey()))) return false;
      }
      for (var term : other.terms.entrySet()) {
        if (containsIdentity(ignored, term.getKey())) continue;
        if (!term.getValue().equals(terms.get(term.getKey()))) return false;
      }
      return true;
    }

    private static boolean containsIdentity(Value[] values, Value target) {
      for (Value value : values) {
        if (value == target) return true;
      }
      return false;
    }
  }

  private static long byteStride(Type sourceType, int operandIndex) {
    Type type = sourceType;
    for (int index = 1; index < operandIndex; index++) {
      if (type.isArray()) type = type.innerType;
    }
    return MemoryLocation.byteSize(type);
  }
}
