package accela.backend;

import accela.backend.target.RISCVTarget;
import accela.backend.target.RISCVTargetOptions;

public class BackendCompiler {
  private final BackendPipeline pipeline;

  public BackendCompiler() {
    this(RISCVTargetOptions.scalarDefault());
  }

  public BackendCompiler(RISCVTargetOptions options) {
    this.pipeline = new BackendPipeline(new RISCVTarget(options));
  }

  public String compileToAssembly(accela.ir.Module module) {
    return pipeline.compileToAssembly(module);
  }
}
