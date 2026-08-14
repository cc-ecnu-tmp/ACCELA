package accela.pass.candidate;

import accela.backend.BackendPipeline;
import accela.backend.instrument.BackendPassInstrumentation;
import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import java.util.List;
import java.util.Objects;
import java.util.function.BiFunction;

/** Candidate descriptor for word-mode facts and safe machine rematerialization. */
public final class Rv64WordPressureCandidate {
  public static final String ID = "candidate.rv64-word-pressure";
  public static final String KNOWN_SEXT32 = ID + ".known-sext32";
  public static final String WORD_OPCODE = ID + ".word-opcode";
  public static final String REMATERIALIZABLE = ID + ".rematerializable";
  public static final String ABI_SAFETY = ID + ".abi-safety";
  public static final List<String> LEGALITY_OBLIGATIONS = List.of(
      KNOWN_SEXT32, WORD_OPCODE, REMATERIALIZABLE, ABI_SAFETY);

  private static final PassDescriptor DESCRIPTOR = new PassDescriptor(
      ID, ID, "RV64GC word-mode pressure relief", PassDescriptor.Stage.BACKEND_FUNCTION, 1,
      PassDescriptor.Lifecycle.CANDIDATE, true,
      new PassDescriptor.CandidateAnchor(
          PassRegistry.BACKEND_REGISTER_ALLOCATION, 1, PassDescriptor.AnchorPosition.BEFORE),
      LEGALITY_OBLIGATIONS);

  private Rv64WordPressureCandidate() {}

  public static PassDescriptor descriptor() { return DESCRIPTOR; }

  public static BiFunction<PassDescriptor, Integer, BackendPipeline.CandidateFunctionPass> factory(
      BackendPassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException("rv64-word-pressure candidate requires instrumentation");
    }
    return (descriptor, occurrence) ->
        new accela.backend.lowering.Rv64WordPressurePass(
            instrumentation, descriptor, occurrence);
  }
}
