package accela.pass.ir.transform.gvn;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.IRBuilder;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.alias.GlobalModRefAnalysis;
import java.util.ArrayList;
import java.util.List;

/** Eliminates redundant pure expressions whose values dominate their uses. */
public final class GVN {
  private GVN() {}

  public static boolean runOnFunction(Function function) {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    var module = function.getModule();
    var modRef = module == null ? null : GlobalModRefAnalysis.analyze(module);
    return runOnFunction(function, fam, modRef);
  }

  private static boolean runOnFunction(
      Function function,
      FunctionAnalysisManager fam,
      GlobalModRefAnalysis.Result modRef) {
    var domTree = fam.getResult(DominatorTreeAnalysis.class, function);
    if (domTree.getEntryBlock() == null) return false;
    return visit(domTree.getEntryBlock(), domTree, new GVNValueTable(), modRef);
  }

  private static boolean visit(
      BasicBlock block,
      DominatorTreeAnalysis.Result domTree,
      GVNValueTable values,
      GlobalModRefAnalysis.Result modRef) {
    List<GVNValueTable.Expression> introduced = new ArrayList<>();
    boolean changed = foldPhiExpressions(block);
    for (Instruction instruction : new ArrayList<>(block.getInstructions())) {
      Value replacement = values.findOrAdd(instruction, introduced, modRef);
      if (replacement == null) continue;
      instruction.replaceAllUsesWith(replacement);
      instruction.eraseFromParent();
      changed = true;
    }
    for (BasicBlock child : domTree.getChildren(block)) {
      changed |= visit(child, domTree, values.copy(), modRef);
    }
    values.leaveScope(introduced);
    return changed;
  }

  /**
   * Performs the PHI-translation case of GVN PRE. If every incoming edge computes the same
   * expression, compute it once in the merge block.
   */
  private static boolean foldPhiExpressions(BasicBlock block) {
    boolean changed = false;
    for (Instruction phi : new ArrayList<>(block.getInstructions())) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      List<Instruction> incoming = equivalentIncomingExpressions(phi);
      if (incoming == null) continue;

      Instruction firstNonPhi = block.getInstructions().stream()
          .filter(instruction -> instruction.getOpcode() != Instruction.Opcode.PHI)
          .findFirst()
          .orElse(null);
      IRBuilder builder = firstNonPhi == null ? new IRBuilder(block) : new IRBuilder();
      if (firstNonPhi != null) builder.setInsertPointBefore(firstNonPhi);
      Instruction expression =
          builder.createBinary(
              incoming.getFirst().getOpcode(),
              incoming.getFirst().getOperand(0),
              incoming.getFirst().getOperand(1));
      phi.replaceAllUsesWith(expression);
      phi.eraseFromParent();
      incoming.forEach(Instruction::eraseFromParent);
      changed = true;
    }
    return changed;
  }

  private static List<Instruction> equivalentIncomingExpressions(Instruction phi) {
    if (phi.getNumOperands() < 4) return null;
    List<Instruction> incoming = new ArrayList<>();
    GVNValueTable.Expression expression = null;
    for (int index = 0; index < phi.getNumOperands(); index += 2) {
      if (!(phi.getOperand(index) instanceof Instruction instruction)
          || instruction.getParent() != phi.getOperand(index + 1)
          || instruction.getNumUses() != 1
          || instruction.getNumOperands() != 2
          || !isPhiTranslatableBinary(instruction)) return null;
      GVNValueTable.Expression current = GVNValueTable.expressionFor(instruction);
      if (expression != null && !expression.equals(current)) return null;
      expression = current;
      incoming.add(instruction);
    }
    return incoming;
  }

  private static boolean isPhiTranslatableBinary(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, SMULH, SDIV, SREM, SHL, ASHR, AND, XOR,
          FADD, FSUB, FMUL, FDIV -> true;
      default -> false;
    };
  }

  public static final class Pass implements FunctionPass {
    private accela.ir.Module cachedModule;
    private GlobalModRefAnalysis.Result modRef;

    @Override
    public PreservedAnalyses run(
        Function function, FunctionAnalysisManager fam) {
      if (function.getModule() != cachedModule) {
        cachedModule = function.getModule();
        modRef = cachedModule == null ? null : GlobalModRefAnalysis.analyze(cachedModule);
      }
      return runOnFunction(function, fam, modRef)
          ? PreservedAnalyses.none()
          : PreservedAnalyses.all();
    }
  }
}
