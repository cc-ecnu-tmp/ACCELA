package accela.pass.ir;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;

/**
 * Adapts a {@link FunctionPassManager} so it can appear inside a module pipeline.
 *
 * <p>Each defined function in the module is visited in source order.
 */
public final class ModuleToFunctionPassAdaptor implements ModulePass {
  private final FunctionPassManager functionPassManager;

  public ModuleToFunctionPassAdaptor(FunctionPassManager functionPassManager) {
    this.functionPassManager = functionPassManager;
  }

  @Override
  public PreservedAnalyses run(
      accela.ir.Module module,
      ModuleAnalysisManager mam,
      FunctionAnalysisManager fam) {
    PreservedAnalyses preserved = PreservedAnalyses.all();
    for (Function function : module.getFunctions()) {
      PreservedAnalyses functionPA = functionPassManager.run(function, fam);
      preserved = preserved.intersect(functionPA);
    }
    // Function transforms may invalidate module-level analyses indirectly, so conservatively
    // preserve only what every nested pass preserved.
    return preserved;
  }
}
