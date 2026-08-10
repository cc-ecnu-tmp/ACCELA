package accela.pass.ir;

import accela.pass.PreservedAnalyses;
import accela.pass.PassDescriptor;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * Runs a sequence of {@link ModulePass} instances over one module.
 *
 * <p>The manager is itself a module pass so it can be treated as a pipeline fragment.
 */
public final class ModulePassManager implements ModulePass {
  private record ScheduledPass(ModulePass pass, PassDescriptor descriptor, int occurrence) {}

  private final List<ScheduledPass> passes = new ArrayList<>();
  private final PassInstrumentation instrumentation;

  public ModulePassManager() {
    this(PassInstrumentation.noop());
  }

  public ModulePassManager(PassInstrumentation instrumentation) {
    this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
  }

  /** Appends one pass to the manager's execution sequence. */
  public void addPass(ModulePass pass) {
    passes.add(new ScheduledPass(Objects.requireNonNull(pass, "pass"), null, 1));
  }

  /** Appends a pass with its stable full-pipeline identity. */
  public void addPass(ModulePass pass, PassDescriptor descriptor, int occurrence) {
    Objects.requireNonNull(pass, "pass");
    Objects.requireNonNull(descriptor, "descriptor");
    if (descriptor.stage() != PassDescriptor.Stage.IR_MODULE) {
      throw new IllegalArgumentException("module pass requires an IR_MODULE descriptor");
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
  public PreservedAnalyses run(
      accela.ir.Module module,
      ModuleAnalysisManager mam,
      FunctionAnalysisManager fam) {
    PreservedAnalyses preserved = PreservedAnalyses.all();
    boolean observed = instrumentation.isEnabled();
    for (ScheduledPass scheduled : passes) {
      if (observed) {
        instrumentation.beforeModulePass(
            scheduled.descriptor(), scheduled.occurrence(), scheduled.pass(), module);
      }
      PreservedAnalyses passPA = scheduled.pass().run(module, mam, fam);
      if (observed) {
        instrumentation.afterModulePass(
            scheduled.descriptor(), scheduled.occurrence(), scheduled.pass(), module,
            passPA.isModified(), passPA);
      }
      // A module transform may change any function. Module-level preserved analyses cannot
      // safely describe a per-function preservation set, so conservatively drop the FAM cache.
      if (passPA.isModified()) fam.invalidateAll();
      mam.invalidate(module, passPA);
      preserved = preserved.intersect(passPA);
    }
    return preserved;
  }
}
