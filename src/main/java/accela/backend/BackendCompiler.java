package accela.backend;

public class BackendCompiler {
  private final BackendPipeline pipeline = new BackendPipeline();

  public String compileToAssembly(accela.ir.Module module) {
    return pipeline.compileToAssembly(module);
  }
}
