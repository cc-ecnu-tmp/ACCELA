package accela.pass.ir.transform.sroa;

import accela.ir.BasicBlock;
import accela.ir.Constant;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Type;
import accela.ir.Use;
import accela.ir.Value;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Scalar-replacement utility for simple array allocas.
 * Handles: array allocas with constant-index GEPs,
 * no address escape, and direct load/store users.
 */
public final class ScalarizeArrayAllocas {
  private ScalarizeArrayAllocas() {}

  public static boolean runOnFunction(Function function) {
    BasicBlock entry = function.getEntryBlock();
    if (entry == null) return false;

    boolean changed = false;
    for (Instruction inst : new ArrayList<>(entry.getInstructions())) {
      if (inst.getOpcode() != Instruction.Opcode.ALLOCA) continue;
      Type allocType = inst.getAllocatedType();
      if (allocType == null || !allocType.isArray()) continue;
      if (splitAlloca(function, inst)) changed = true;
    }
    return changed;
  }

  private static boolean splitAlloca(Function function, Instruction alloca) {
    SplitPlan plan = analyzeAlloca(alloca);
    if (plan == null) return false;

    BasicBlock entry = function.getEntryBlock();
    Map<Integer, Instruction> scalarAllocas = new LinkedHashMap<>();
    IRBuilder entryBuilder = new IRBuilder();
    for (int leafIndex : plan.touchedLeaves) {
      Instruction scalarAlloca = entryBuilder.createAllocaInEntry(plan.scalarType, entry);
      String baseName = alloca.getName() != null ? alloca.getName() : "sroa";
      scalarAlloca.setName(baseName + ".sroa." + leafIndex);
      scalarAllocas.put(leafIndex, scalarAlloca);
    }

    for (Instruction zeroStore : plan.zeroStores) {
      IRBuilder builder = new IRBuilder();
      builder.setInsertPointBefore(zeroStore);
      Value zero = Constant.zero(plan.scalarType);
      for (Instruction scalarAlloca : scalarAllocas.values()) {
        builder.createStore(zero, scalarAlloca);
      }
      zeroStore.eraseFromParent();
    }

    for (Map.Entry<Instruction, Integer> entryRewrite : plan.gepToLeafIndex.entrySet()) {
      Instruction gep = entryRewrite.getKey();
      Instruction scalarAlloca = scalarAllocas.get(entryRewrite.getValue());
      if (scalarAlloca == null) {
        throw new IllegalStateException("SROA missing scalar slot for touched leaf");
      }
      gep.replaceAllUsesWith(scalarAlloca);
      gep.eraseFromParent();
    }

    if (alloca.hasUses()) {
      throw new IllegalStateException("SROA left residual uses on split alloca");
    }
    alloca.eraseFromParent();
    return true;
  }

  private static SplitPlan analyzeAlloca(Instruction alloca) {
    Type allocType = alloca.getAllocatedType();
    if (allocType == null || !allocType.isArray()) return null;

    SplitPlan plan = new SplitPlan(allocType.scalarType(), countLeafElements(allocType));
    if (plan.leafCount <= 0) return null;

    for (Use use : new ArrayList<>(alloca.getUses())) {
      Instruction user = use.getUser();
      if (user.getOpcode() == Instruction.Opcode.STORE && user.getOperand(1) == alloca) {
        if (use.getOperandIndex() != 1) return null;
        if (!(user.getOperand(0) instanceof Constant.Zero zero) || zero.getType() != allocType) {
          return null;
        }
        plan.zeroStores.add(user);
        continue;
      }

      if (user.getOpcode() == Instruction.Opcode.GEP && user.getOperand(0) == alloca) {
        if (use.getOperandIndex() != 0) return null;
        Integer leafIndex = resolveLeafIndex(user, allocType, plan.leafCount);
        if (leafIndex == null || !hasOnlyDirectScalarMemoryUses(user)) return null;
        plan.gepToLeafIndex.put(user, leafIndex);
        plan.touchedLeaves.add(leafIndex);
        continue;
      }

      return null;
    }

    if (plan.zeroStores.isEmpty() && plan.gepToLeafIndex.isEmpty()) {
      return null;
    }
    return plan;
  }

  private static boolean hasOnlyDirectScalarMemoryUses(Instruction gep) {
    for (Use use : gep.getUses()) {
      Instruction user = use.getUser();
      if (user.getOpcode() == Instruction.Opcode.LOAD && use.getOperandIndex() == 0) continue;
      if (user.getOpcode() == Instruction.Opcode.STORE && use.getOperandIndex() == 1) continue;
      return false;
    }
    return true;
  }

  private static Integer resolveLeafIndex(Instruction gep, Type allocaType, int totalLeaves) {
    Type scalarType = allocaType.scalarType();
    Type sourceType = gep.getGepSourceType();
    if (sourceType == null) return null;

    if (sourceType == scalarType && gep.getNumOperands() == 2) {
      Long flatIndex = constantIndex(gep.getOperand(1));
      if (flatIndex == null || flatIndex < 0 || flatIndex >= totalLeaves) return null;
      return flatIndex.intValue();
    }

    if (sourceType != allocaType || gep.getNumOperands() < 3) return null;
    Long leadingZero = constantIndex(gep.getOperand(1));
    if (leadingZero == null || leadingZero != 0) return null;

    int leafIndex = 0;
    Type curType = allocaType;
    for (int i = 2; i < gep.getNumOperands(); i++) {
      if (!curType.isArray()) return null;
      Long idx = constantIndex(gep.getOperand(i));
      if (idx == null || idx < 0 || idx >= curType.size) return null;
      leafIndex += idx.intValue() * countLeafElements(curType.innerType);
      curType = curType.innerType;
    }

    if (curType.isArray()) return null;
    return leafIndex;
  }

  private static Long constantIndex(Value value) {
    if (value instanceof Constant.Int constant) return constant.value;
    return null;
  }

  private static int countLeafElements(Type type) {
    if (!type.isArray()) return 1;
    return type.size * countLeafElements(type.innerType);
  }

  private static final class SplitPlan {
    final Type scalarType;
    final int leafCount;
    final Set<Integer> touchedLeaves = new LinkedHashSet<>();
    final List<Instruction> zeroStores = new ArrayList<>();
    final Map<Instruction, Integer> gepToLeafIndex = new LinkedHashMap<>();

    SplitPlan(Type scalarType, int leafCount) {
      this.scalarType = scalarType;
      this.leafCount = leafCount;
    }
  }
}
