package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Instruction.Opcode;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Bottom-up SLP vectorizer modeled on LLVM's {@code SLPVectorizer}.
 *
 * <p>Starting from consecutive scalar stores in a basic block, the pass builds isomorphic
 * instruction trees lane-by-lane and rewrites them into fixed-width vector loads, arithmetic, and
 * stores. The mid-end keeps vector IR until {@link VectorScalarization}; this pass therefore
 * exposes vector semantics early while remaining safe when the backend expands vectors back to
 * scalar RISC-V code.
 */
public final class SLPVectorizer {
  private static final int MIN_WIDTH = 2;
  private static final int MAX_WIDTH = 4;

  private SLPVectorizer() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    boolean changed = false;
    for (BasicBlock block : function.getBlocks()) {
      changed |= vectorizeBlock(block);
    }
    return changed;
  }

  private static boolean vectorizeBlock(BasicBlock block) {
    boolean changed = false;
    Set<Instruction> consumed = new HashSet<>();

    while (true) {
      StoreCluster cluster = findBestCluster(block, consumed);
      if (cluster == null) break;
      if (rewriteCluster(cluster)) {
        changed = true;
        consumed.addAll(cluster.stores());
      } else {
        consumed.add(cluster.stores().getFirst());
      }
    }
    return changed;
  }

  private static StoreCluster findBestCluster(BasicBlock block, Set<Instruction> consumed) {
    StoreCluster best = null;
    for (Instruction instruction : block.getInstructions()) {
      if (instruction.getOpcode() != Opcode.STORE
          || consumed.contains(instruction)
          || instruction.getOperand(0).getType().isVector()) continue;

      StoreCluster cluster = findClusterFromStore(block, instruction, consumed);
      if (cluster != null && (best == null || cluster.width() > best.width())) {
        best = cluster;
      }
    }
    return best;
  }

  private static StoreCluster findClusterFromStore(
      BasicBlock block, Instruction seed, Set<Instruction> consumed) {
    Type elementType = seed.getOperand(0).getType();
    if (elementType != Type.INT && elementType != Type.FLOAT) return null;

    SLPAddress seedAddress = SLPAddress.forAccess(seed.getOperand(1), elementType);
    if (seedAddress == null || seedAddress.isStackAccess()) return null;

    List<StoreEntry> entries = new ArrayList<>();
    for (Instruction instruction : block.getInstructions()) {
      if (instruction.getOpcode() != Opcode.STORE
          || consumed.contains(instruction)
          || !instruction.getOperand(0).getType().equals(elementType)) continue;
      SLPAddress address = SLPAddress.forAccess(instruction.getOperand(1), elementType);
      if (address == null
          || address.base != seedAddress.base
          || !address.elementType.equals(seedAddress.elementType)) continue;
      entries.add(new StoreEntry(instruction, address));
    }
    entries.sort(Comparator.comparingLong(entry -> entry.address().elementIndex));

    int seedIndex = -1;
    for (int index = 0; index < entries.size(); index++) {
      if (entries.get(index).instruction() == seed) {
        seedIndex = index;
        break;
      }
    }
    if (seedIndex < 0) return null;

    int begin = seedIndex;
    int end = seedIndex;
    while (begin > 0
        && entries.get(begin - 1).address().isConsecutiveWith(entries.get(begin).address())) {
      begin--;
    }
    while (end + 1 < entries.size()
        && entries.get(end).address().isConsecutiveWith(entries.get(end + 1).address())) {
      end++;
    }

    List<Instruction> run = entries.subList(begin, end + 1).stream()
        .map(StoreEntry::instruction)
        .toList();
    int seedOffset = seedIndex - begin;

    for (int width = Math.min(run.size(), MAX_WIDTH); width >= MIN_WIDTH; width--) {
      int windowStart = Math.max(0, seedOffset - width + 1);
      int windowLimit = Math.min(windowStart + 1, run.size() - width + 1);
      for (int start = windowStart; start < windowLimit; start++) {
        List<Instruction> slice = run.subList(start, start + width);
        StoreCluster cluster = StoreCluster.tryCreate(slice, elementType);
        if (cluster != null) return cluster;
      }
    }
    return null;
  }

  private record StoreEntry(Instruction instruction, SLPAddress address) {}

  private static boolean rewriteCluster(StoreCluster cluster) {
    Map<Value, Value> laneToVector = new IdentityHashMap<>();
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(earliestStore(cluster.stores()));

    Value vectorValue = vectorizeLaneValues(
        cluster.stores().stream().map(store -> store.getOperand(0)).toList(),
        cluster.elementType(),
        cluster.width(),
        builder,
        laneToVector);
    if (vectorValue == null) return false;

    builder.createStore(vectorValue, cluster.stores().getFirst().getOperand(1));

    for (Instruction store : cluster.stores()) store.eraseFromParent();
    for (Instruction dead : cluster.members()) {
      if (!dead.hasUses()) dead.eraseFromParent();
    }
    return true;
  }

  private static Instruction earliestStore(List<Instruction> stores) {
    Instruction earliest = stores.getFirst();
    BasicBlock block = earliest.getParent();
    for (Instruction instruction : block.getInstructions()) {
      if (stores.contains(instruction)) return instruction;
    }
    return earliest;
  }

  private static Value vectorizeLaneValues(
      List<Value> laneValues,
      Type elementType,
      int width,
      IRBuilder builder,
      Map<Value, Value> laneToVector) {
    List<Value> cached = laneValues.stream().map(laneToVector::get).toList();
    if (cached.stream().allMatch(value -> value != null && value.getType().isVector())) {
      return cached.getFirst();
    }

    if (laneValues.stream().distinct().count() == 1) {
      Value shared = laneValues.getFirst();
      Type vectorType = Type.vector(elementType, width);
      if (shared instanceof Constant constant) {
        Value vector = Constant.vector(vectorType, List.copyOf(java.util.Collections.nCopies(width, constant)));
        laneValues.forEach(lane -> laneToVector.put(lane, vector));
        return vector;
      }
      if (shared.getType().equals(elementType)) {
        Value vector = builder.createSplat(vectorType, shared);
        laneValues.forEach(lane -> laneToVector.put(lane, vector));
        return vector;
      }
    }

    if (laneValues.stream().allMatch(value -> value instanceof Constant)) {
      Type vectorType = Type.vector(elementType, width);
      Value vector = Constant.vector(
          vectorType,
          laneValues.stream().map(value -> (Constant) value).toList());
      for (Value lane : laneValues) laneToVector.put(lane, vector);
      return vector;
    }

    if (!laneValues.stream().allMatch(value -> value instanceof Instruction)) return null;
    Instruction prototype = (Instruction) laneValues.getFirst();
    for (Value lane : laneValues) {
      if (!matchesShape(prototype, (Instruction) lane)) return null;
    }

    if (prototype.getOpcode() == Opcode.LOAD) {
      SLPAddress first = SLPAddress.forAccess(prototype.getOperand(0), elementType);
      if (first == null) return null;
      SLPAddress previous = first;
      for (int lane = 1; lane < laneValues.size(); lane++) {
        Instruction load = (Instruction) laneValues.get(lane);
        SLPAddress address = SLPAddress.forAccess(load.getOperand(0), elementType);
        if (address == null || !previous.isConsecutiveWith(address)) return null;
        previous = address;
      }
      Type vectorType = Type.vector(elementType, width);
      Value vector = builder.createLoad(vectorType, prototype.getOperand(0));
      for (Value lane : laneValues) laneToVector.put(lane, vector);
      return vector;
    }

    if (!isVectorizableOpcode(prototype.getOpcode())) return null;

    Value[] vectorOperands = new Value[prototype.getNumOperands()];
    for (int operand = 0; operand < prototype.getNumOperands(); operand++) {
      List<Value> lanes = new ArrayList<>();
      for (Value laneValue : laneValues) {
        lanes.add(((Instruction) laneValue).getOperand(operand));
      }
      vectorOperands[operand] = vectorizeLaneValues(
          lanes, elementType, width, builder, laneToVector);
      if (vectorOperands[operand] == null) return null;
    }

    Value vector = createVectorOperation(builder, prototype, vectorOperands);
    if (vector == null) return null;
    for (Value lane : laneValues) laneToVector.put(lane, vector);
    return vector;
  }

  private static Value createVectorOperation(
      IRBuilder builder, Instruction prototype, Value[] operands) {
    return switch (prototype.getOpcode()) {
      case ADD -> builder.createAdd(operands[0], operands[1]);
      case SUB -> builder.createSub(operands[0], operands[1]);
      case MUL -> builder.createMul(operands[0], operands[1]);
      case FADD -> builder.createFAdd(operands[0], operands[1]);
      case FSUB -> builder.createFSub(operands[0], operands[1]);
      case FMUL -> builder.createFMul(operands[0], operands[1]);
      case FNEG -> builder.createFNeg(operands[0]);
      default -> null;
    };
  }

  private static boolean matchesShape(Instruction left, Instruction right) {
    if (left.getOpcode() != right.getOpcode()
        || !left.getType().equals(right.getType())
        || left.getNumOperands() != right.getNumOperands()) return false;
    if (left.getOpcode() == Opcode.ICMP || left.getOpcode() == Opcode.FCMP) {
      return left.getPredicate().equals(right.getPredicate());
    }
    return true;
  }

  private static boolean isVectorizableOpcode(Opcode opcode) {
    return switch (opcode) {
      case LOAD, ADD, SUB, MUL, FADD, FSUB, FMUL, FNEG -> true;
      default -> false;
    };
  }

  private record StoreCluster(List<Instruction> stores, Type elementType, int width, Set<Instruction> members) {
    static StoreCluster tryCreate(List<Instruction> stores, Type elementType) {
      int width = stores.size();
      Set<Instruction> members = new LinkedHashSet<>();
      List<Value> roots = stores.stream().map(store -> store.getOperand(0)).toList();
      if (!gatherTree(roots, elementType, members)) return null;
      if (!isClosed(members, stores)) return null;
      return new StoreCluster(List.copyOf(stores), elementType, width, members);
    }

    private static boolean gatherTree(
        List<Value> values, Type elementType, Set<Instruction> members) {
      if (values.stream().allMatch(value -> value instanceof Constant || !(value instanceof Instruction))) {
        return true;
      }
      if (!values.stream().allMatch(value -> value instanceof Instruction)) return false;

      Instruction prototype = (Instruction) values.getFirst();
      if (prototype.getOpcode() == Opcode.PHI
          || prototype.getOpcode() == Opcode.SELECT
          || prototype.getOpcode() == Opcode.CALL) return false;
      if (!isVectorizableOpcode(prototype.getOpcode())) return false;

      for (Value value : values) {
        if (!matchesShape(prototype, (Instruction) value)) return false;
      }

      if (prototype.getOpcode() == Opcode.LOAD) {
        SLPAddress first = SLPAddress.forAccess(prototype.getOperand(0), elementType);
        if (first == null) return false;
        SLPAddress previous = first;
        for (int lane = 1; lane < values.size(); lane++) {
          Instruction load = (Instruction) values.get(lane);
          SLPAddress address = SLPAddress.forAccess(load.getOperand(0), elementType);
          if (address == null || !previous.isConsecutiveWith(address)) return false;
          previous = address;
        }
      }

      members.addAll(values.stream().map(value -> (Instruction) value).toList());

      for (int operand = 0; operand < prototype.getNumOperands(); operand++) {
        if (prototype.getOpcode() == Opcode.LOAD && operand == 0) continue;
        List<Value> operandLanes = new ArrayList<>();
        for (Value value : values) {
          operandLanes.add(((Instruction) value).getOperand(operand));
        }
        if (!gatherTree(operandLanes, elementType, members)) return false;
      }
      return true;
    }

    private static boolean isClosed(Set<Instruction> members, List<Instruction> stores) {
      Set<Instruction> storeSet = new HashSet<>(stores);
      for (Instruction member : members) {
        for (var use : member.getUses()) {
          Instruction user = use.getUser();
          if (user.getOpcode() == Opcode.STORE && storeSet.contains(user)) continue;
          if (!members.contains(user)) return false;
        }
      }
      return true;
    }
  }

  /** Constant element offset from a pointer root for one scalar access. */
  private static final class SLPAddress {
    private final Value base;
    private final long elementIndex;
    private final Type elementType;

    private SLPAddress(Value base, long elementIndex, Type elementType) {
      this.base = base;
      this.elementIndex = elementIndex;
      this.elementType = elementType;
    }

    static SLPAddress forAccess(Value pointer, Type elementType) {
      Value current = pointer;
      Type sourceType = elementType;
      List<Long> indices = new ArrayList<>();

      while (current instanceof Instruction gep && gep.getOpcode() == Opcode.GEP) {
        sourceType = gep.getGepSourceType();
        for (int operand = 1; operand < gep.getNumOperands(); operand++) {
          Long index = constantIndex(gep.getOperand(operand));
          if (index == null) return null;
          indices.add(index);
        }
        current = gep.getOperand(0);
      }

      long linearIndex = indices.isEmpty()
          ? 0
          : linearElementIndex(sourceType, indices, elementType);
      if (linearIndex < 0) return null;
      return new SLPAddress(current, linearIndex, elementType);
    }

    boolean isConsecutiveWith(SLPAddress other) {
      return base == other.base
          && elementType.equals(other.elementType)
          && other.elementIndex == elementIndex + 1;
    }

    boolean isStackAccess() {
      return base instanceof Instruction instruction
          && instruction.getOpcode() == Opcode.ALLOCA;
    }

    private static long linearElementIndex(Type sourceType, List<Long> indices, Type elementType) {
      if (!sourceType.isArray()) {
        if (indices.size() != 1 || !sourceType.equals(elementType)) return -1;
        return indices.getFirst();
      }
      long index = 0;
      Type current = sourceType;
      for (long subscript : indices) {
        if (!current.isArray()) return -1;
        if (subscript < 0 || subscript >= current.size) return -1;
        index = Math.multiplyExact(index + subscript, leafCount(current.innerType));
        current = current.innerType;
      }
      return current.equals(elementType) ? index : -1;
    }

    private static int leafCount(Type type) {
      return type.isArray() ? type.size * leafCount(type.innerType) : 1;
    }

    private static Long constantIndex(Value value) {
      return value instanceof Constant.Int integer ? integer.value : null;
    }
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return SLPVectorizer.run(function, fam)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
