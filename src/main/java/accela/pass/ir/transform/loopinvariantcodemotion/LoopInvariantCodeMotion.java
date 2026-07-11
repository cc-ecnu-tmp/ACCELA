package accela.pass.ir.transform.loopinvariantcodemotion;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.ir.Value;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** Hoists safe, memory-free loop-invariant instructions into loop preheaders. */
public final class LoopInvariantCodeMotion {
  private LoopInvariantCodeMotion() {}

  public static boolean run(Function function, FunctionAnalysisManager fam) {
    List<LoopAnalysis.Loop> loops = new ArrayList<>(
        fam.getResult(LoopAnalysis.class, function).loops());
    loops.sort(Comparator.comparingInt(loop -> loop.blocks().size()));
    boolean changed = false;
    for (LoopAnalysis.Loop loop : loops) {
      BasicBlock preheader = loop.preheader();
      if (preheader == null || preheader.getTerminator() == null) continue;
      boolean localChange;
      do {
        localChange = false;
        for (BasicBlock block : loop.blocks()) {
          for (Instruction instruction : List.copyOf(block.getInstructions())) {
            if (!isSafeToHoist(instruction) || !operandsAreInvariant(instruction, loop)) continue;
            block.remove(instruction);
            preheader.insertInstructionBefore(preheader.getTerminator(), instruction);
            localChange = true;
            changed = true;
          }
        }
      } while (localChange);
    }
    return changed;
  }

  private static boolean operandsAreInvariant(
      Instruction instruction, LoopAnalysis.Loop loop) {
    for (int index = 0; index < instruction.getNumOperands(); index++) {
      Value operand = instruction.getOperand(index);
      if (operand instanceof Instruction definition
          && loop.blocks().contains(definition.getParent())) return false;
    }
    return true;
  }

  private static boolean isSafeToHoist(Instruction instruction) {
    return switch (instruction.getOpcode()) {
      case ADD, SUB, MUL, ICMP, FCMP, GEP, ZEXT, SEXT, SITOFP, FPTOSI, XOR -> true;
      default -> false;
    };
  }

  public static final class Pass implements FunctionPass {
    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      return LoopInvariantCodeMotion.run(function, fam)
          ? PreservedAnalyses.none() : PreservedAnalyses.all();
    }
  }
}
