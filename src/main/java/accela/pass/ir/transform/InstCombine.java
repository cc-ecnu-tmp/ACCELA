package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.IRBuilder;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Combines instruction trees into equivalent, cheaper IR forms. */
public final class InstCombine {
  private InstCombine() {}

  public static boolean runOnFunction(Function function) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction phi : List.copyOf(block.getInstructions())) {
        if (phi.getOpcode() != Instruction.Opcode.PHI) break;
        changed |= narrowBooleanPhi(phi);
      }
    }
    boolean combined;
    do {
      combined = false;
      for (BasicBlock block : function.getBlocks()) {
        for (Instruction instruction : List.copyOf(block.getInstructions())) {
          combined |= foldAddSubTree(instruction);
        }
      }
      changed |= combined;
    } while (combined);
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : List.copyOf(block.getInstructions())) {
        changed |= foldPowerOfTwoRemainderTest(instruction);
      }
    }
    for (BasicBlock block : function.getBlocks()) {
      for (Instruction instruction : List.copyOf(block.getInstructions())) {
        changed |= foldExactPowerOfTwoDivision(instruction);
        changed |= foldVectorLaneOperation(instruction);
        changed |= foldVectorRebuild(instruction);
        changed |= foldIdentityShuffle(instruction);
        changed |= foldRedundantInsert(instruction);
      }
    }
    return changed;
  }

  /** Forwards constant-lane extracts through vector constructors and updates. */
  private static boolean foldVectorLaneOperation(Instruction extract) {
    if (extract.getOpcode() != Instruction.Opcode.EXTRACT_ELEMENT
        || !(extract.getOperand(1) instanceof Constant.Int lane)) return false;
    int index = (int) lane.value;
    Type vectorType = extract.getOperand(0).getType();
    if (index < 0 || index >= vectorType.getLaneCount()) return false;
    Value replacement = null;
    if (extract.getOperand(0) instanceof Constant.Vector vectorConstant) {
      replacement = vectorConstant.elements.get(index);
    } else if (extract.getOperand(0) instanceof Constant.Zero) {
      replacement = scalarZero(vectorType.getElementType());
    } else if (!(extract.getOperand(0) instanceof Instruction vector)) {
      return false;
    } else if (vector.getOpcode() == Instruction.Opcode.BUILD_VECTOR
        && index >= 0 && index < vector.getNumOperands()) {
      replacement = vector.getOperand(index);
    } else if (vector.getOpcode() == Instruction.Opcode.SPLAT) {
      replacement = vector.getOperand(0);
    } else if (vector.getOpcode() == Instruction.Opcode.INSERT_ELEMENT
        && vector.getOperand(2) instanceof Constant.Int insertedLane) {
      if (insertedLane.value == lane.value) {
        replacement = vector.getOperand(1);
      } else {
        IRBuilder builder = new IRBuilder();
        builder.setInsertPointBefore(extract);
        replacement = builder.createExtractElement(vector.getOperand(0), lane);
      }
    }
    if (replacement == null) return false;
    extract.replaceAllUsesWith(replacement);
    extract.eraseFromParent();
    return true;
  }

  /** Removes a vector rebuilt lane-for-lane from the same source vector. */
  private static boolean foldVectorRebuild(Instruction build) {
    if (build.getOpcode() != Instruction.Opcode.BUILD_VECTOR) return false;
    Value source = null;
    for (int lane = 0; lane < build.getNumOperands(); lane++) {
      if (!(build.getOperand(lane) instanceof Instruction extract)
          || extract.getOpcode() != Instruction.Opcode.EXTRACT_ELEMENT
          || !(extract.getOperand(1) instanceof Constant.Int index)
          || index.value != lane) return false;
      if (source == null) source = extract.getOperand(0);
      else if (source != extract.getOperand(0)) return false;
    }
    if (source == null || !source.getType().equals(build.getType())) return false;
    build.replaceAllUsesWith(source);
    build.eraseFromParent();
    return true;
  }

  /** Removes shuffles whose mask selects one input unchanged. */
  private static boolean foldIdentityShuffle(Instruction shuffle) {
    if (shuffle.getOpcode() != Instruction.Opcode.SHUFFLE_VECTOR
        || !(shuffle.getOperand(2) instanceof Constant.Vector mask)) return false;
    int lanes = shuffle.getOperand(0).getType().getLaneCount();
    if (shuffle.getType().getLaneCount() != lanes) return false;
    boolean leftIdentity = true;
    boolean rightIdentity = true;
    for (int lane = 0; lane < lanes; lane++) {
      if (!(mask.elements.get(lane) instanceof Constant.Int index)) return false;
      leftIdentity &= index.value == lane;
      rightIdentity &= index.value == lanes + lane;
    }
    Value replacement = leftIdentity ? shuffle.getOperand(0)
        : rightIdentity ? shuffle.getOperand(1) : null;
    if (replacement == null) return false;
    shuffle.replaceAllUsesWith(replacement);
    shuffle.eraseFromParent();
    return true;
  }

  /** Removes insertion of a lane extracted unchanged from the same vector. */
  private static boolean foldRedundantInsert(Instruction insert) {
    if (insert.getOpcode() != Instruction.Opcode.INSERT_ELEMENT
        || !(insert.getOperand(1) instanceof Instruction extract)
        || extract.getOpcode() != Instruction.Opcode.EXTRACT_ELEMENT
        || extract.getOperand(0) != insert.getOperand(0)
        || !(insert.getOperand(2) instanceof Constant.Int insertedLane)
        || !(extract.getOperand(1) instanceof Constant.Int extractedLane)
        || insertedLane.value != extractedLane.value
        || insertedLane.value < 0
        || insertedLane.value >= insert.getType().getLaneCount()) return false;
    insert.replaceAllUsesWith(insert.getOperand(0));
    insert.eraseFromParent();
    return true;
  }

  private static Constant scalarZero(Type type) {
    if (type == Type.FLOAT) return Constant.floatConst(0.0f);
    if (type == Type.I1) return Constant.boolConst(false);
    if (type == Type.I64) return Constant.int64Const(0);
    return Constant.intConst(0);
  }

  /** Cancels repeated terms in a bounded i32 add/sub tree when that reduces instruction count. */
  private static boolean foldAddSubTree(Instruction root) {
    if (root.getType() != Type.INT
        || (root.getOpcode() != Instruction.Opcode.ADD
            && root.getOpcode() != Instruction.Opcode.SUB)) return false;

    LinearExpression expression = new LinearExpression();
    if (!expression.collect(root, 1) || !expression.hasUnitCoefficients()) return false;
    long removable =
        expression.operations.stream()
            .filter(
                operation ->
                    operation == root
                        || operation.getUses().stream()
                            .allMatch(use -> expression.operations.contains(use.getUser())))
            .count();
    if (expression.cost() >= removable) return false;

    Value replacement = expression.buildBefore(root);
    root.replaceAllUsesWith(replacement);
    root.eraseFromParent();
    return true;
  }

  private static final class LinearExpression {
    private static final int MAX_VISITS = 64;
    private final Map<Value, Integer> terms = new LinkedHashMap<>();
    private final Set<Instruction> operations =
        Collections.newSetFromMap(new IdentityHashMap<>());
    private int constant;
    private int visits;

    private boolean collect(Value value, int sign) {
      if (++visits > MAX_VISITS) return false;
      if (value instanceof Constant.Int integer) {
        constant += sign * (int) integer.value;
        return true;
      }
      if (value instanceof Instruction operation
          && operation.getType() == Type.INT
          && (operation.getOpcode() == Instruction.Opcode.ADD
              || operation.getOpcode() == Instruction.Opcode.SUB)) {
        operations.add(operation);
        return collect(operation.getOperand(0), sign)
            && collect(
                operation.getOperand(1),
                operation.getOpcode() == Instruction.Opcode.ADD ? sign : -sign);
      }
      terms.merge(value, sign, Integer::sum);
      return true;
    }

    private boolean hasUnitCoefficients() {
      return terms.values().stream().allMatch(coefficient -> Math.abs(coefficient) <= 1);
    }

    private int cost() {
      int count = (int) terms.values().stream().filter(coefficient -> coefficient != 0).count();
      boolean hasPositive = terms.values().stream().anyMatch(coefficient -> coefficient > 0);
      return count == 0 ? 0 : count - (constant == 0 && hasPositive ? 1 : 0);
    }

    private Value buildBefore(Instruction root) {
      IRBuilder builder = new IRBuilder();
      builder.setInsertPointBefore(root);
      Value result = constant == 0 ? null : Constant.intConst(constant);
      for (var term : terms.entrySet()) {
        if (term.getValue() <= 0) continue;
        if (result == null) {
          result = term.getKey();
        } else {
          result = builder.createAdd(result, term.getKey());
        }
      }
      for (var term : terms.entrySet()) {
        if (term.getValue() < 0) {
          result = builder.createSub(result == null ? Constant.intConst(0) : result, term.getKey());
        }
      }
      return result == null ? Constant.intConst(0) : result;
    }
  }

  /** Replaces equality tests of a power-of-two signed remainder with a bit mask. */
  private static boolean foldPowerOfTwoRemainderTest(Instruction compare) {
    if (compare.getOpcode() != Instruction.Opcode.ICMP
        || !(compare.getPredicate().equals("eq") || compare.getPredicate().equals("ne"))) {
      return false;
    }
    int valueIndex =
        compare.getOperand(1) instanceof Constant.Int ? 0
            : compare.getOperand(0) instanceof Constant.Int ? 1 : -1;
    if (valueIndex < 0 || !(compare.getOperand(valueIndex) instanceof Instruction remainder)
        || remainder.getOpcode() != Instruction.Opcode.SREM || remainder.getNumUses() != 1) {
      return false;
    }
    Long divisor = positivePowerOfTwo(remainder.getOperand(1));
    long expected = ((Constant.Int) compare.getOperand(1 - valueIndex)).value;
    if (divisor == null || expected < 0 || expected >= divisor) return false;

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(compare);
    long mask = divisor - 1;
    // A positive signed remainder also requires a non-negative dividend.
    if (expected != 0) mask |= Integer.MIN_VALUE;
    Value masked = builder.createAnd(remainder.getOperand(0), Constant.intConst(mask));
    compare.setOperand(valueIndex, masked);
    remainder.eraseFromParent();
    return true;
  }

  /**
   * On an edge where {@code x % 2^k == 0}, truncating signed division is exactly an arithmetic
   * shift: {@code x / 2^k -> x >> k}.
   */
  private static boolean foldExactPowerOfTwoDivision(Instruction division) {
    if (division.getOpcode() != Instruction.Opcode.SDIV) return false;
    Long divisor = positivePowerOfTwo(division.getOperand(1));
    if (divisor == null) return false;
    BasicBlock block = division.getParent();
    List<BasicBlock> predecessors = block.getPredecessors();
    if (predecessors.size() != 1
        || !edgeProvesMultiple(
            predecessors.getFirst(), block, division.getOperand(0), divisor)) return false;

    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(division);
    Value shift =
        builder.createAShr(
            division.getOperand(0), Constant.intConst(Long.numberOfTrailingZeros(divisor)));
    division.replaceAllUsesWith(shift);
    division.eraseFromParent();
    return true;
  }

  private static boolean edgeProvesMultiple(
      BasicBlock predecessor, BasicBlock target, Value dividend, long divisor) {
    Instruction branch = predecessor.getTerminator();
    if (branch == null || branch.getOpcode() != Instruction.Opcode.CONDBR
        || !(branch.getOperand(0) instanceof Instruction compare)
        || compare.getOpcode() != Instruction.Opcode.ICMP) return false;
    boolean zeroOnTrue = compare.getPredicate().equals("eq");
    if (!zeroOnTrue && !compare.getPredicate().equals("ne")) return false;
    if (branch.getOperand(zeroOnTrue ? 1 : 2) != target) return false;

    Value tested = isZero(compare.getOperand(1)) ? compare.getOperand(0)
        : isZero(compare.getOperand(0)) ? compare.getOperand(1) : null;
    if (!(tested instanceof Instruction operation)) return false;
    if (operation.getOpcode() == Instruction.Opcode.SREM) {
      return operation.getOperand(0) == dividend
          && isConstant(operation.getOperand(1), divisor);
    }
    return operation.getOpcode() == Instruction.Opcode.AND
        && ((operation.getOperand(0) == dividend
                && isConstant(operation.getOperand(1), divisor - 1))
            || (operation.getOperand(1) == dividend
                && isConstant(operation.getOperand(0), divisor - 1)));
  }

  private static Long positivePowerOfTwo(Value value) {
    if (!(value instanceof Constant.Int integer)) return null;
    long divisor = integer.value;
    return divisor > 0 && divisor <= Integer.MAX_VALUE
        && (divisor & (divisor - 1)) == 0 ? divisor : null;
  }

  private static boolean isConstant(Value value, long expected) {
    return value instanceof Constant.Int integer && integer.value == expected;
  }

  /**
   * Implements the boolean case of LLVM's foldPHIArgZextsIntoPHI:
   * {@code icmp ne (phi 0, zext i1 %x), 0} becomes {@code phi false, %x}.
   */
  private static boolean narrowBooleanPhi(Instruction phi) {
    if (phi.getType() != Type.INT || phi.getNumUses() != 1) return false;
    var use = phi.getUses().getFirst();
    Instruction compare = use.getUser();
    if (compare.getOpcode() != Instruction.Opcode.ICMP
        || !compare.getPredicate().equals("ne")
        || !comparesWithZero(compare, phi)) return false;

    Instruction narrow = Instruction.createPhi(Type.I1);
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      Value incoming = narrowIncoming(phi.getOperand(index));
      if (incoming == null) return false;
      narrow.addOperand(incoming);
      narrow.addOperand(phi.getOperand(index + 1));
    }
    phi.getParent().addInstructionToFront(narrow);
    compare.replaceAllUsesWith(narrow);
    compare.eraseFromParent();
    phi.eraseFromParent();
    return true;
  }

  private static boolean comparesWithZero(Instruction compare, Instruction phi) {
    return (compare.getOperand(0) == phi && isZero(compare.getOperand(1)))
        || (compare.getOperand(1) == phi && isZero(compare.getOperand(0)));
  }

  private static Value narrowIncoming(Value value) {
    if (value instanceof Constant.Int integer) {
      return integer.value == 0 || integer.value == 1
          ? Constant.boolConst(integer.value != 0)
          : null;
    }
    if (value instanceof Instruction extension
        && extension.getOpcode() == Instruction.Opcode.ZEXT
        && extension.getOperand(0).getType() == Type.I1) {
      return extension.getOperand(0);
    }
    return null;
  }

  private static boolean isZero(Value value) {
    return value instanceof Constant.Int integer && integer.value == 0;
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return runOnFunction(function) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
