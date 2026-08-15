package accela.pass.ir;

import accela.pass.PreservedAnalyses;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.List;

/**
 * Runs a sequence of {@link ModulePass} instances over one module.
 *
 * <p>The manager is itself a module pass so it can be treated as a pipeline fragment.
 */
public final class ModulePassManager implements ModulePass {
  private final List<ModulePass> passes = new ArrayList<>();
  private final PassInstrumentation instrumentation;

  public ModulePassManager() {
    this(PassInstrumentation.noop());
  }

  public ModulePassManager(PassInstrumentation instrumentation) {
    this.instrumentation = instrumentation;
  }

  /** Appends one pass to the manager's execution sequence. */
  public void addPass(ModulePass pass) {
    passes.add(pass);
  }

  /** Returns whether this manager currently has no passes. */
  public boolean isEmpty() {
    return passes.isEmpty();
  }

  @Override
  public PreservedAnalyses run(
      accela.ir.Module module,
      ModuleAnalysisManager mam,
      FunctionAnalysisManager fam) {
    PreservedAnalyses preserved = PreservedAnalyses.all();
    for (ModulePass pass : passes) {
      instrumentation.beforeModulePass(pass, module);
      PreservedAnalyses passPA = pass.run(module, mam, fam);
      instrumentation.afterModulePass(pass, module, passPA);
      mam.invalidate(module, passPA);
      preserved = preserved.intersect(passPA);
    }
    return preserved;
  }
}
