package accela.pass.ir.analysis.dependence;

import accela.ir.Constant;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Value;
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

  /**
   * Cross-loop equality result used by sequential-loop fusion.
   *
   * <p>{@code firstMinusSecond} is null only when every iteration of the two loops addresses the
   * same location. That all-to-all case is a real dependence, not an unknown result.
   */
  record FusionDistance(Relation relation, Long firstMinusSecond) {}

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

  /**
   * Solves equality after treating two distinct SSA inductions as the same iteration coordinate.
   *
   * <p>The caller has already proved that the loops have identical, positive unit-step domains.
   * A positive returned distance means that an access from a later first-loop iteration aliases an
   * access from an earlier second-loop iteration, which sequential fusion would reorder.
   */
  FusionDistance fusionDistanceTo(
      AffineAccess other, Value firstInduction, Value secondInduction) {
    if (indices.size() != other.indices.size()) {
      return new FusionDistance(Relation.UNKNOWN, null);
    }
    Long solvedDistance = null;
    boolean unknown = false;
    for (int index = 0; index < indices.size(); index++) {
      FusionDistance constraint = indices.get(index).fusionDistanceTo(
          other.indices.get(index), firstInduction, secondInduction);
      if (constraint.relation() == Relation.DISJOINT) {
        return constraint;
      }
      if (constraint.relation() == Relation.UNKNOWN) {
        unknown = true;
        continue;
      }
      if (constraint.firstMinusSecond() == null) continue;
      if (solvedDistance != null
          && solvedDistance.longValue() != constraint.firstMinusSecond().longValue()) {
        return new FusionDistance(Relation.DISJOINT, null);
      }
      solvedDistance = constraint.firstMinusSecond();
    }
    return new FusionDistance(unknown ? Relation.UNKNOWN : Relation.DEPENDENT, solvedDistance);
  }

  boolean provesDistinctAtLanes(
      AffineAccess other,
      int variedLoop,
      int independentLoop,
      int thisLane,
      int otherLane,
      long step) {
    if (indices.size() != other.indices.size()) return false;
    for (int index = 0; index < indices.size(); index++) {
      Long distance = indices.get(index).distanceAtLanes(
          other.indices.get(index),
          inductions,
          variedLoop,
          independentLoop,
          thisLane,
          otherLane,
          step);
      if (distance != null && distance != 0) return true;
    }
    return false;
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

    private FusionDistance fusionDistanceTo(
        AffineIndex other, Value firstInduction, Value secondInduction) {
      if (byteStride != other.byteStride || !analyzable || !other.analyzable) {
        return new FusionDistance(Relation.UNKNOWN, null);
      }
      if (coefficient(secondInduction) != 0
          || other.coefficient(firstInduction) != 0
          || !sameTermsExcept(other, firstInduction, secondInduction)) {
        return new FusionDistance(Relation.UNKNOWN, null);
      }
      long firstCoefficient = coefficient(firstInduction);
      long secondCoefficient = other.coefficient(secondInduction);
      if (firstCoefficient != secondCoefficient) {
        return new FusionDistance(Relation.UNKNOWN, null);
      }
      try {
        long offsetDelta = Math.subtractExact(other.offset, offset);
        if (firstCoefficient == 0) {
          return offsetDelta == 0
              ? new FusionDistance(Relation.DEPENDENT, null)
              : new FusionDistance(Relation.DISJOINT, null);
        }
        if (offsetDelta == Long.MIN_VALUE && firstCoefficient == -1) {
          return new FusionDistance(Relation.UNKNOWN, null);
        }
        if (offsetDelta % firstCoefficient != 0) {
          return new FusionDistance(Relation.DISJOINT, null);
        }
        return new FusionDistance(
            Relation.DEPENDENT, offsetDelta / firstCoefficient);
      } catch (ArithmeticException overflow) {
        return new FusionDistance(Relation.UNKNOWN, null);
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
          || coefficient(loops.get(independentLoop)) != 0
          || other.coefficient(loops.get(independentLoop)) != 0
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
    return sizeOf(type);
  }

  private static long sizeOf(Type type) {
    if (type.isArray()) return Math.multiplyExact(type.size, sizeOf(type.innerType));
    if (type == Type.I64 || type.isPointer()) return 8;
    return type == Type.I1 ? 1 : 4;
  }
}
