package accela.backend;

import accela.cost.DecisionTraceSink;
import accela.cost.GeneratedTargetProfile;

public class BackendCompiler {
  private final BackendPipeline pipeline;

  public BackendCompiler() {
    this(DecisionTraceSink.noop());
  }

  public BackendCompiler(DecisionTraceSink trace) {
    pipeline = new BackendPipeline(GeneratedTargetProfile.get(), trace);
  }

  public String compileToAssembly(accela.ir.Module module) {
    return pipeline.compileToAssembly(module);
  }
}
