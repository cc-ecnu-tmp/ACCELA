package accela.pass.ir.transform;

import accela.ir.Constant;
import accela.ir.ConstantFolding;
import accela.ir.GlobalVariable;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Module;
import accela.ir.Type;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import java.util.List;

/** Interprocedural sparse conditional constant propagation for whole SysY programs. */
public final class IPSCCP {
  private IPSCCP() {}

  static boolean runOnModule(Module module) {
    boolean changed = propagateGlobals(module);
    return new IPSCCPSolver(module).solve() | changed;
  }

  private static boolean propagateGlobals(Module module) {
    boolean changed = false;
    for (GlobalVariable global : module.getGlobals()) {
      Constant initializer = global.getInitializer();
      if (initializer == null) continue;
      if (global.getValueType().isArray()) {
        if (global.isConstant()) changed |= propagateConstantArray(global);
        continue;
      }
      if (!global.isConstant() && !hasOnlyDirectLoads(global)) continue;
      for (var use : List.copyOf(global.getUses())) {
        Instruction load = use.getUser();
        if (use.getOperandIndex() != 0
            || load.getOpcode() != Instruction.Opcode.LOAD) continue;
        load.replaceAllUsesWith(initializer);
        load.eraseFromParent();
        changed = true;
      }
    }
    return changed;
  }

  private static boolean propagateConstantArray(GlobalVariable global) {
    boolean changed = false;
    boolean powerOfTwoTable = isPowerOfTwoTable(global);
    for (var use : List.copyOf(global.getUses())) {
      if (use.getOperandIndex() != 0
          || use.getUser().getOpcode() != Instruction.Opcode.GEP) continue;
      Instruction gep = use.getUser();
      Integer index = ConstantFolding.constantArrayIndex(global, gep);
      Value replacement = index == null ? powerOfTwoIndex(gep, global, powerOfTwoTable) : null;
      for (var gepUse : List.copyOf(gep.getUses())) {
        Instruction load = gepUse.getUser();
        if (gepUse.getOperandIndex() != 0
            || load.getOpcode() != Instruction.Opcode.LOAD) continue;
        Value value = index == null
            ? replacement : ConstantFolding.initializerAt(global, index);
        if (value == null) continue;
        load.replaceAllUsesWith(value);
        load.eraseFromParent();
        changed = true;
      }
      if (!gep.hasUses()) gep.eraseFromParent();
    }
    return changed;
  }

  private static Value powerOfTwoIndex(
      Instruction gep, GlobalVariable global, boolean powerOfTwoTable) {
    if (!powerOfTwoTable
        || gep.getGepSourceType() != global.getValueType()
        || gep.getNumOperands() != 3
        || !(gep.getOperand(1) instanceof Constant.Int zero)
        || zero.value != 0) return null;
    Value index = gep.getOperand(2);
    if (index instanceof Instruction extension
        && extension.getOpcode() == Instruction.Opcode.SEXT
        && extension.getOperand(0).getType() == Type.INT) {
      index = extension.getOperand(0);
    }
    if (index.getType() != Type.INT) return null;
    IRBuilder builder = new IRBuilder();
    builder.setInsertPointBefore(gep);
    return builder.createShl(Constant.intConst(1), index);
  }

  private static boolean isPowerOfTwoTable(GlobalVariable global) {
    Type type = global.getValueType();
    if (!type.isArray()
        || type.innerType != Type.INT
        || type.size > 31
        || !(global.getInitializer() instanceof Constant.Array array)
        || array.elements.size() != type.size) return false;
    for (int index = 0; index < type.size; index++) {
      if (!(array.elements.get(index) instanceof Constant.Int value)
          || value.value != 1L << index) return false;
    }
    return true;
  }

  private static boolean hasOnlyDirectLoads(GlobalVariable global) {
    return global.getUses().stream().allMatch(use ->
        use.getOperandIndex() == 0
            && use.getUser().getOpcode() == Instruction.Opcode.LOAD);
  }

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
