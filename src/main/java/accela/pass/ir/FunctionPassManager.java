package accela.pass.ir;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.List;

/**
 * Runs a sequence of {@link FunctionPass} instances over one function.
 *
 * <p>The manager is itself a function pass so it can be nested or adapted into larger pipelines.
 */
public final class FunctionPassManager implements FunctionPass {
  private final List<FunctionPass> passes = new ArrayList<>();
  private final PassInstrumentation instrumentation;

  public FunctionPassManager() {
    this(PassInstrumentation.noop());
  }

  public FunctionPassManager(PassInstrumentation instrumentation) {
    this.instrumentation = instrumentation;
  }

  /** Appends one pass to the manager's execution sequence. */
  public void addPass(FunctionPass pass) {
    passes.add(pass);
  }

  /** Returns whether this manager currently has no passes. */
  public boolean isEmpty() {
    return passes.isEmpty();
  }

  @Override
  public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
    PreservedAnalyses preserved = PreservedAnalyses.all();
    for (FunctionPass pass : passes) {
      instrumentation.beforeFunctionPass(pass, function);
      PreservedAnalyses passPA = pass.run(function, fam);
      instrumentation.afterFunctionPass(pass, function, passPA);
      fam.invalidate(function, passPA);
      preserved = preserved.intersect(passPA);
    }
    return preserved;
  }
}
