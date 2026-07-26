package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.ConstantFolding;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import accela.pass.ir.analysis.alias.PointerProvenance;
import java.util.ArrayList;
import java.util.List;

/** Removes overwritten stores and stores to global locations that are never read. */
public final class DeadStoreElimination {
  private DeadStoreElimination() {}

  static boolean runOnModule(accela.ir.Module module) {
    GlobalModRefAnalysis.Result modRef = GlobalModRefAnalysis.analyze(module);
    boolean changed = false;
    for (Function function : module.getFunctions()) {
      for (BasicBlock block : function.getBlocks()) {
        changed |= runOnBlock(block, modRef);
      }
    }
    return removeNeverReadGlobalStores(module, modRef) | changed;
  }

  private static boolean removeNeverReadGlobalStores(
      accela.ir.Module module, GlobalModRefAnalysis.Result modRef) {
    List<Instruction> instructions = module.getFunctions().stream()
        .flatMap(function -> function.getBlocks().stream())
        .flatMap(block -> block.getInstructions().stream())
        .toList();
    boolean changed = false;
    for (Instruction instruction : instructions) {
      if (instruction.getOpcode() != Instruction.Opcode.STORE) continue;
      Value pointer = instruction.getOperand(1);
      if (!(PointerProvenance.root(pointer) instanceof GlobalVariable)) continue;
      boolean read = instructions.stream().anyMatch(candidate ->
          (candidate.getOpcode() == Instruction.Opcode.LOAD
                  && mayAliasLocation(pointer, candidate.getOperand(0)))
              || (candidate.getOpcode() == Instruction.Opcode.CALL
                  && modRef.mayRead(candidate, pointer)));
      if (read) continue;
      instruction.eraseFromParent();
      changed = true;
    }
    return changed;
  }

  private static boolean mayAliasLocation(Value left, Value right) {
    if (!PointerProvenance.mayAlias(left, right)) return false;
    Location leftLocation = constantLocation(left);
    Location rightLocation = constantLocation(right);
    return leftLocation == null
        || rightLocation == null
        || (leftLocation.global == rightLocation.global
            && leftLocation.leaf == rightLocation.leaf);
  }

  private static Location constantLocation(Value pointer) {
    if (!(pointer instanceof Instruction gep)
        || !(PointerProvenance.root(pointer) instanceof GlobalVariable global)) return null;
    Integer leaf = ConstantFolding.constantArrayIndex(global, gep);
    return leaf == null ? null : new Location(global, leaf);
  }

  private record Location(GlobalVariable global, int leaf) {}

  private static boolean runOnBlock(
      BasicBlock block, GlobalModRefAnalysis.Result modRef) {
    List<Value> laterStores = new ArrayList<>();
    boolean changed = false;
    List<Instruction> instructions = List.copyOf(block.getInstructions());
    for (int index = instructions.size() - 1; index >= 0; index--) {
      Instruction instruction = instructions.get(index);
      if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
        Value pointer = instruction.getOperand(0);
        laterStores.removeIf(store -> PointerProvenance.mayAlias(store, pointer));
      } else if (instruction.getOpcode() == Instruction.Opcode.CALL) {
        laterStores.removeIf(pointer -> modRef.mayRead(instruction, pointer));
      } else if (instruction.getOpcode() == Instruction.Opcode.STORE) {
        Value pointer = instruction.getOperand(1);
        if (laterStores.contains(pointer)) {
          instruction.eraseFromParent();
          changed = true;
        } else {
          laterStores.add(pointer);
        }
      }
    }
    return changed;
  }

  public static final class Pass implements ModulePass {
    @Override
    public PreservedAnalyses run(
        accela.ir.Module module,
        ModuleAnalysisManager mam,
        FunctionAnalysisManager fam) {
      return runOnModule(module) ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
