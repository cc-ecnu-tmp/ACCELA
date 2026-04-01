package accela.pass.ir.transform.sroa;

import accela.ir.Function;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.transform.PromoteMemoryToRegister;

/**
 * Aggressively promotes remaining scalar allocas during SROA.
 *
 * <p>Reuses mem2reg utility: once parameter copy slots are removed and array allocas are
 * scalarized, the remaining simple scalar stack slots are excellent candidates for promotion.
 */
public final class PromoteScalarAllocas {
  private PromoteScalarAllocas() {}

  public static boolean runOnFunction(Function function, DominatorTreeAnalysis.Result domTree) {
    return PromoteMemoryToRegister.run(function, domTree);
  }
}
