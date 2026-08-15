package accela.pass.ir.transform;

import accela.ir.Function;
import accela.pass.PassBuilder;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPassManager;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.gvn.GVN;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;
import accela.pass.ir.verify.IRVerifier;

/** Runs scalar optimizations after vector lanes have become ordinary SSA values. */
public final class ScalarizedVectorCleanup {
  private ScalarizedVectorCleanup() {}

  public static boolean run(accela.ir.Module module) {
    PassBuilder passBuilder = new PassBuilder();
    FunctionAnalysisManager fam = passBuilder.buildFunctionAnalysisManager();
    FunctionPassManager cleanup =
        new FunctionPassManager(PassInstrumentation.enabled(false));
    cleanup.addPass(new SCCP.Pass());
    cleanup.addPass(new EarlyCSE.Pass());
    cleanup.addPass(new InstSimplify.Pass());
    cleanup.addPass(new InstCombine.Pass());
    cleanup.addPass(new GVN.Pass());
    cleanup.addPass(new ADCE.Pass());
    cleanup.addPass(new SimplifyCFG.Pass());

    boolean changed = false;
    for (Function function : module.getFunctions()) {
      int before = instructionCount(function);
      cleanup.run(function, fam);
      changed |= instructionCount(function) != before;
    }
    IRVerifier.verifyModule(module);
    return changed;
  }

  private static int instructionCount(Function function) {
    return function.getBlocks().stream().mapToInt(block -> block.getInstructions().size()).sum();
  }
}
