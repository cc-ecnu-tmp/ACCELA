package accela.pass.ir.transform;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.GlobalVariable;
import accela.ir.Instruction;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.analysis.MemoryLocation;
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
      MemoryLocation location = MemoryLocation.fromInstruction(instruction);
      if (!(PointerProvenance.root(location.pointer()) instanceof GlobalVariable)) continue;
      boolean read = instructions.stream().anyMatch(candidate ->
          (candidate.getOpcode() == Instruction.Opcode.LOAD
                  && mayAliasLocation(
                      location, MemoryLocation.fromInstruction(candidate)))
              || (candidate.getOpcode() == Instruction.Opcode.CALL
                  && modRef.mayRead(candidate, location.pointer())));
      if (read) continue;
      instruction.eraseFromParent();
      changed = true;
    }
    return changed;
  }

  private static boolean mayAliasLocation(MemoryLocation left, MemoryLocation right) {
    if (!PointerProvenance.mayAlias(left.pointer(), right.pointer())) return false;
    return !left.isKnownDisjoint(right);
  }

  private static boolean runOnBlock(
      BasicBlock block, GlobalModRefAnalysis.Result modRef) {
    List<MemoryLocation> laterStores = new ArrayList<>();
    boolean changed = false;
    List<Instruction> instructions = List.copyOf(block.getInstructions());
    for (int index = instructions.size() - 1; index >= 0; index--) {
      Instruction instruction = instructions.get(index);
      if (instruction.getOpcode() == Instruction.Opcode.LOAD) {
        MemoryLocation load = MemoryLocation.fromInstruction(instruction);
        laterStores.removeIf(store -> mayAliasLocation(store, load));
      } else if (instruction.getOpcode() == Instruction.Opcode.CALL) {
        laterStores.removeIf(
            location -> modRef.mayRead(instruction, location.pointer()));
      } else if (instruction.getOpcode() == Instruction.Opcode.STORE) {
        MemoryLocation store = MemoryLocation.fromInstruction(instruction);
        if (laterStores.stream().anyMatch(later -> fullyCovers(later, store))) {
          instruction.eraseFromParent();
          changed = true;
        } else {
          laterStores.add(store);
        }
      }
    }
    return changed;
  }

  private static boolean fullyCovers(MemoryLocation later, MemoryLocation earlier) {
    return later.isKnownToCover(earlier);
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
