package accela.backend;

import accela.backend.instrument.BackendPassInstrumentation;
import accela.pass.PipelineProfile;
import java.util.Objects;

/** Public entry point for the RISC-V backend. */
public class BackendCompiler {
  private final BackendPipeline pipeline;

  /** Constructs the production FULL backend with no benchmark overhead. */
  public BackendCompiler() {
    this(PipelineProfile.full(), BackendPassInstrumentation.noop());
  }

  /** Constructs an explicitly profiled backend for evaluation tooling. */
  public BackendCompiler(
      PipelineProfile profile, BackendPassInstrumentation instrumentation) {
    this(profile, instrumentation, BackendPipeline.CandidatePassProvider.empty());
  }

  public BackendCompiler(
      PipelineProfile profile,
      BackendPassInstrumentation instrumentation,
      BackendPipeline.CandidatePassProvider candidatePassProvider) {
    pipeline = new BackendPipeline(
        Objects.requireNonNull(profile, "profile"),
        Objects.requireNonNull(instrumentation, "instrumentation"),
        Objects.requireNonNull(candidatePassProvider, "candidatePassProvider"));
  }

  public String compileToAssembly(accela.ir.Module module) {
    return pipeline.compileToAssembly(module);
  }
}
