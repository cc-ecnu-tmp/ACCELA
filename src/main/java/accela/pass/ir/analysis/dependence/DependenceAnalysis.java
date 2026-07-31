package accela.pass.ir.analysis.dependence;

import accela.ir.BasicBlock;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Computes conservative affine memory dependences for a canonical loop nest.
 *
 * <p>Like LLVM's DependenceAnalysis, ordered flow, anti, and output dependences carry one
 * direction per loop level. Unsupported pointer arithmetic is represented by {@link Direction#ANY}
 * and therefore cannot justify a loop transformation.
 */
public final class DependenceAnalysis {
  public enum Direction {
    LESS,
    EQUAL,
    GREATER,
    ANY;

    private Direction reversed() {
      return switch (this) {
        case LESS -> GREATER;
        case GREATER -> LESS;
        case EQUAL, ANY -> this;
      };
    }
  }

  public enum Kind {
    FLOW,
    ANTI,
    OUTPUT
  }

  public record Dependence(
      Instruction source,
      Instruction sink,
      Kind kind,
      List<Direction> directions) {
    public Dependence {
      directions = List.copyOf(directions);
    }
  }

  public static final class Result {
    private final List<Value> inductionValues;
    private final List<MemoryAccess> accesses;
    private final List<Dependence> dependences;
    private final boolean unknownMemory;

    private Result(
        List<Value> inductionValues,
        List<MemoryAccess> accesses,
        List<Dependence> dependences,
        boolean unknownMemory) {
      this.inductionValues = List.copyOf(inductionValues);
      this.accesses = List.copyOf(accesses);
      this.dependences = List.copyOf(dependences);
      this.unknownMemory = unknownMemory;
    }

    public List<Dependence> dependences() {
      return dependences;
    }

    /** Applies the direction-vector permutation theorem used by LLVM LoopInterchange. */
    public boolean isLegalToInterchange(int outerLoop, int innerLoop) {
      if (unknownMemory) return false;
      for (Dependence dependence : dependences) {
        List<Direction> permuted = new ArrayList<>(dependence.directions());
        Collections.swap(permuted, outerLoop, innerLoop);
        if (!isLexicographicallyNonNegative(permuted)) return false;
      }
      return true;
    }

    /**
     * Proves that adjacent outer lanes access disjoint objects independently of the inner IV.
     */
    public boolean isSafeToJam(
        int variedLoop, int independentLoop, long step, int factor) {
      if (unknownMemory) return false;
      for (int left = 0; left < accesses.size(); left++) {
        for (int right = left; right < accesses.size(); right++) {
          MemoryAccess first = accesses.get(left);
          MemoryAccess second = accesses.get(right);
          if (!conflicts(first, second)) continue;
          if (first.formula() == null
              || second.formula() == null
              || first.formula().root() != second.formula().root()) return false;
          for (int firstLane = 0; firstLane < factor; firstLane++) {
            for (int secondLane = 0; secondLane < factor; secondLane++) {
              if (firstLane == secondLane) continue;
              if (!first.formula().provesDistinctAtLanes(
                  second.formula(),
                  variedLoop,
                  independentLoop,
                  firstLane,
                  secondLane,
                  step)) {
                return false;
              }
            }
          }
        }
      }
      return true;
    }

    /** Same proof using the actual induction values, avoiding caller-dependent loop numbering. */
    public boolean isSafeToJam(
        Value variedInduction, Value independentInduction, long step, int factor) {
      int variedLoop = inductionValues.indexOf(variedInduction);
      int independentLoop = inductionValues.indexOf(independentInduction);
      if (variedLoop < 0 || independentLoop < 0 || variedLoop == independentLoop) return false;
      return isSafeToJam(variedLoop, independentLoop, step, factor);
    }

    /** Estimates spatial locality from the byte stride of each memory instruction. */
    public long localityCost(int loop) {
      long cost = 0;
      for (MemoryAccess access : accesses) {
        Long stride = access.formula() == null ? null : access.formula().byteStride(loop);
        if (stride == null || stride == Long.MIN_VALUE) return Long.MAX_VALUE;
        long weight = access.write() ? 2 : 1;
        cost = Math.addExact(cost, weight * Math.min(Math.abs(stride), 64));
      }
      return cost;
    }

    private static boolean isLexicographicallyNonNegative(List<Direction> directions) {
      for (Direction direction : directions) {
        if (direction == Direction.EQUAL) continue;
        return direction == Direction.LESS;
      }
      return true;
    }
  }

  private DependenceAnalysis() {}

  public static Result analyze(
      List<Value> inductionValues, List<BasicBlock> blocks) {
    List<MemoryAccess> accesses = new ArrayList<>();
    boolean unknownMemory = false;
    for (BasicBlock block : blocks) {
      for (Instruction instruction : block.getInstructions()) {
        Value pointer = memoryPointer(instruction);
        if (pointer != null) {
          accesses.add(new MemoryAccess(
              instruction,
              pointer,
              instruction.getOpcode() == Instruction.Opcode.STORE,
              AffineAccess.match(pointer, inductionValues)));
        } else if (instruction.getOpcode() == Instruction.Opcode.CALL) {
          unknownMemory = true;
        }
      }
    }

    List<Dependence> dependences = new ArrayList<>();
    for (int left = 0; left < accesses.size(); left++) {
      for (int right = left; right < accesses.size(); right++) {
        MemoryAccess source = accesses.get(left);
        MemoryAccess sink = accesses.get(right);
        if (!conflicts(source, sink)) continue;
        Dependence dependence = dependence(source, sink, inductionValues.size());
        if (dependence != null) dependences.add(dependence);
      }
    }
    return new Result(inductionValues, accesses, dependences, unknownMemory);
  }

  private static Dependence dependence(
      MemoryAccess source, MemoryAccess sink, int loopCount) {
    if (source.formula() == null
        || sink.formula() == null
        || source.formula().root() != sink.formula().root()) {
      return unknownDependence(source, sink, loopCount);
    }
    AffineAccess.Distance distance = source.formula().distanceTo(sink.formula());
    if (distance.relation() == AffineAccess.Relation.DISJOINT) return null;
    if (distance.relation() == AffineAccess.Relation.UNKNOWN) {
      return unknownDependence(source, sink, loopCount);
    }

    List<Direction> directions = new ArrayList<>();
    for (Long value : distance.distances()) {
      directions.add(value == null
          ? Direction.ANY
          : value > 0 ? Direction.LESS : value < 0 ? Direction.GREATER : Direction.EQUAL);
    }
    boolean reversed = firstKnownDirection(directions) == Direction.GREATER;
    if (reversed) directions.replaceAll(Direction::reversed);
    return new Dependence(
        reversed ? sink.instruction() : source.instruction(),
        reversed ? source.instruction() : sink.instruction(),
        kind(reversed ? sink : source, reversed ? source : sink),
        directions);
  }

  private static Dependence unknownDependence(
      MemoryAccess source, MemoryAccess sink, int loopCount) {
    return new Dependence(
        source.instruction(),
        sink.instruction(),
        kind(source, sink),
        Collections.nCopies(loopCount, Direction.ANY));
  }

  private static Direction firstKnownDirection(List<Direction> directions) {
    for (Direction direction : directions) {
      if (direction == Direction.LESS || direction == Direction.GREATER) return direction;
      if (direction == Direction.ANY) return Direction.ANY;
    }
    return Direction.EQUAL;
  }

  private static Kind kind(MemoryAccess source, MemoryAccess sink) {
    if (source.write() && sink.write()) return Kind.OUTPUT;
    return source.write() ? Kind.FLOW : Kind.ANTI;
  }

  private static boolean conflicts(MemoryAccess first, MemoryAccess second) {
    return (first.write() || second.write())
        && PointerProvenance.mayAlias(first.pointer(), second.pointer());
  }

  private static Value memoryPointer(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case LOAD -> instruction.getOperand(0);
      case STORE -> instruction.getOperand(1);
      default -> null;
    };
  }

  private record MemoryAccess(
      Instruction instruction, Value pointer, boolean write, AffineAccess formula) {}
}
