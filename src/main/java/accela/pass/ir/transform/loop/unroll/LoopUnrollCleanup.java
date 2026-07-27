package accela.pass.ir.transform.loop.unroll;

import accela.ir.Function;
import accela.pass.ir.transform.ADCE;
import accela.pass.ir.transform.InstSimplify;
import accela.pass.ir.transform.SCCP;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;

/** Runs the canonical simplifications exposed by full unrolling. */
final class LoopUnrollCleanup {
  private LoopUnrollCleanup() {}

  static void run(Function function) {
    SCCP.runOnFunction(function);
    InstSimplify.runOnFunction(function);
    ADCE.runOnFunction(function);
    SimplifyCFG.runOnFunction(function);
  }
}
