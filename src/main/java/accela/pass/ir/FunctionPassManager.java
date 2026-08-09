package accela.pass.ir;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.PassDescriptor;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * Runs a sequence of {@link FunctionPass} instances over one function.
 *
 * <p>The manager is itself a function pass so it can be nested or adapted into larger pipelines.
 */
public final class FunctionPassManager implements FunctionPass {
  private record ScheduledPass(FunctionPass pass, PassDescriptor descriptor, int occurrence) {}

  private final List<ScheduledPass> passes = new ArrayList<>();
  private final PassInstrumentation instrumentation;

  public FunctionPassManager() {
    this(PassInstrumentation.noop());
  }

  public FunctionPassManager(PassInstrumentation instrumentation) {
    this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
  }

  /** Appends one pass to the manager's execution sequence. */
  public void addPass(FunctionPass pass) {
    passes.add(new ScheduledPass(Objects.requireNonNull(pass, "pass"), null, 1));
  }

  /** Appends a pass with its stable full-pipeline identity. */
  public void addPass(FunctionPass pass, PassDescriptor descriptor, int occurrence) {
    Objects.requireNonNull(pass, "pass");
    Objects.requireNonNull(descriptor, "descriptor");
    if (descriptor.stage() != PassDescriptor.Stage.IR_FUNCTION) {
      throw new IllegalArgumentException("function pass requires an IR_FUNCTION descriptor");
    }
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException("invalid occurrence " + occurrence + " for "
          + descriptor.id());
    }
    passes.add(new ScheduledPass(pass, descriptor, occurrence));
  }

  /** Returns whether this manager currently has no passes. */
  public boolean isEmpty() {
    return passes.isEmpty();
  }

  @Override
  public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
    PreservedAnalyses preserved = PreservedAnalyses.all();
    boolean observed = instrumentation.isEnabled();
    for (ScheduledPass scheduled : passes) {
      if (observed) {
        instrumentation.beforeFunctionPass(
            scheduled.descriptor(), scheduled.occurrence(), scheduled.pass(), function);
      }
      PreservedAnalyses passPA = scheduled.pass().run(function, fam);
      if (observed) {
        instrumentation.afterFunctionPass(
            scheduled.descriptor(), scheduled.occurrence(), scheduled.pass(), function,
            passPA.isModified(), passPA);
      }
      fam.invalidate(function, passPA);
      preserved = preserved.intersect(passPA);
    }
    return preserved;
  }
}
