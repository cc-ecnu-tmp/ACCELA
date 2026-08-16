package accela.backend;

import accela.cost.DecisionTraceSink;
import accela.cost.GeneratedTargetProfile;
import accela.pass.R2PipelineProfile;

public class BackendCompiler {
  private final BackendPipeline pipeline;

  public BackendCompiler() {
    this(DecisionTraceSink.noop());
  }

  public BackendCompiler(DecisionTraceSink trace) {
    pipeline = new BackendPipeline(GeneratedTargetProfile.get(), trace);
  }

  public BackendCompiler(DecisionTraceSink trace, R2PipelineProfile profile) {
    pipeline = new BackendPipeline(GeneratedTargetProfile.get(), trace, profile);
  }

  public String compileToAssembly(accela.ir.Module module) {
    return pipeline.compileToAssembly(module);
  }
}
