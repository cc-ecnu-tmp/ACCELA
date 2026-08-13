package accela.backend.instrument;

import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineModule;
import accela.backend.regalloc.AllocationResult;
import accela.pass.PassDescriptor;
import accela.pass.instrument.DecisionObservability;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.instrument.PassRemark;
import accela.pass.instrument.PassRemarkSink;
import accela.pass.ir.instrument.IRMetrics;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.function.BooleanSupplier;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

/** Timing and before/after metrics for registered backend stages. */
public final class BackendPassInstrumentation implements AutoCloseable {
  private static final BackendPassInstrumentation NOOP =
      new BackendPassInstrumentation(false, PassRemarkSink.noop(), () -> 0L);

  private final boolean enabled;
  private final PassRemarkSink sink;
  private final LongSupplier nanoTime;
  private final Thread ownerThread;
  private boolean closed;

  public static BackendPassInstrumentation noop() {
    return NOOP;
  }

  public static BackendPassInstrumentation observed(PassRemarkSink sink) {
    return new BackendPassInstrumentation(
        true, Objects.requireNonNull(sink, "sink"), System::nanoTime);
  }

  /** Returns whether benchmark observations are active for this compilation. */
  public boolean isEnabled() {
    return enabled;
  }

  /** Creates a fail-fast decision emitter for one observed backend candidate target. */
  public PassDecisionEmitter decisionEmitter(
      PassDescriptor descriptor, int occurrence, String targetKind, String targetName) {
    if (!enabled) {
      throw new IllegalStateException("backend pass decision observation is disabled");
    }
    checkOwnerAndOpen();
    validateBackendDescriptor(descriptor, occurrence);
    if (!descriptor.decisionObservable()) {
      throw new IllegalArgumentException(
          "pass '" + descriptor.id() + "' is not registered as decision-observable");
    }
    return sink.decisionEmitter(descriptor, occurrence, targetKind, targetName);
  }

  static BackendPassInstrumentation observed(
      PassRemarkSink sink, LongSupplier nanoTime) {
    return new BackendPassInstrumentation(
        true,
        Objects.requireNonNull(sink, "sink"),
        Objects.requireNonNull(nanoTime, "nanoTime"));
  }

  private BackendPassInstrumentation(
      boolean enabled, PassRemarkSink sink, LongSupplier nanoTime) {
    this.enabled = enabled;
    this.sink = sink;
    this.nanoTime = nanoTime;
    this.ownerThread = enabled ? Thread.currentThread() : null;
  }

  public MachineModule lower(
      PassDescriptor descriptor,
      int occurrence,
      accela.ir.Module source,
      Supplier<MachineModule> operation) {
    if (!enabled) return operation.get();
    validate(descriptor, PassDescriptor.Stage.BACKEND_MODULE, occurrence);
    checkOwnerAndOpen();
    Map<String, Long> before = prefixed("ir.", IRMetrics.capture(source).asMap());
    long started = nanoTime.getAsLong();
    MachineModule result = operation.get();
    long elapsed = Math.max(0L, nanoTime.getAsLong() - started);
    emit(descriptor, occurrence, "module", "<module>", elapsed, true, before,
        prefixed("machine.", MachineMetrics.capture(result).asMap()), Map.of());
    return result;
  }

  public void runFunction(
      PassDescriptor descriptor,
      int occurrence,
      MachineFunction function,
      BooleanSupplier operation) {
    if (!enabled) {
      operation.getAsBoolean();
      return;
    }
    validate(descriptor, PassDescriptor.Stage.BACKEND_FUNCTION, occurrence);
    checkOwnerAndOpen();
    Map<String, Long> before = MachineMetrics.capture(function).asMap();
    long started = nanoTime.getAsLong();
    boolean modified = operation.getAsBoolean();
    long elapsed = Math.max(0L, nanoTime.getAsLong() - started);
    emit(descriptor, occurrence, "machine_function", "@" + function.getName(), elapsed, modified,
        before, MachineMetrics.capture(function).asMap(), Map.of());
  }

  public AllocationResult allocate(
      PassDescriptor descriptor,
      int occurrence,
      MachineFunction function,
      Supplier<AllocationResult> operation) {
    if (!enabled) return operation.get();
    validate(descriptor, PassDescriptor.Stage.BACKEND_FUNCTION, occurrence);
    checkOwnerAndOpen();
    Map<String, Long> before = MachineMetrics.capture(function).asMap();
    long started = nanoTime.getAsLong();
    AllocationResult result = operation.get();
    long elapsed = Math.max(0L, nanoTime.getAsLong() - started);
    LinkedHashMap<String, Long> details = new LinkedHashMap<>();
    details.put("allocated_values", (long) result.getLocations().size());
    details.put("register_locations",
        result.getLocations().values().stream().filter(location -> location.isRegister()).count());
    details.put("stack_locations",
        result.getLocations().values().stream().filter(location -> location.isStack()).count());
    details.put("used_callee_saved_registers",
        (long) result.getUsedCalleeSavedRegisters().size());
    emit(descriptor, occurrence, "machine_function", "@" + function.getName(), elapsed, true,
        before, MachineMetrics.capture(function).asMap(), details);
    return result;
  }

  public String emitAssembly(
      PassDescriptor descriptor,
      int occurrence,
      MachineModule module,
      Supplier<String> operation) {
    return emitAssembly(descriptor, occurrence, module, operation, () -> false, () -> {});
  }

  /**
   * Times only assembly generation. The modification probe and post-timing observation execute
   * after the clock is stopped, so metric collection and JSONL I/O cannot pollute pass time.
   */
  public String emitAssembly(
      PassDescriptor descriptor,
      int occurrence,
      MachineModule module,
      Supplier<String> operation,
      BooleanSupplier modificationProbe,
      Runnable postTimingObservation) {
    if (!enabled) return operation.get();
    validate(descriptor, PassDescriptor.Stage.BACKEND_MODULE, occurrence);
    checkOwnerAndOpen();
    Objects.requireNonNull(modificationProbe, "modificationProbe");
    Objects.requireNonNull(postTimingObservation, "postTimingObservation");
    Map<String, Long> before = MachineMetrics.capture(module).asMap();
    long started = nanoTime.getAsLong();
    String assembly = operation.get();
    long elapsed = Math.max(0L, nanoTime.getAsLong() - started);
    Map<String, Long> after = MachineMetrics.capture(module).asMap();
    boolean modified = modificationProbe.getAsBoolean();
    LinkedHashMap<String, Long> details = new LinkedHashMap<>();
    details.put("assembly_chars", (long) assembly.length());
    details.put("assembly_lines", assembly.lines().count());
    postTimingObservation.run();
    emit(descriptor, occurrence, "module", "<module>", elapsed, modified, before, after, details);
    return assembly;
  }

  /** Records an optional module-scoped algorithm embedded inside assembly selection. */
  public void observeEmbeddedModulePass(
      PassDescriptor descriptor,
      int occurrence,
      MachineModule module,
      long elapsedNanos,
      boolean modified,
      Map<String, Long> details) {
    if (!enabled) return;
    validate(descriptor, PassDescriptor.Stage.BACKEND_MODULE, occurrence);
    checkOwnerAndOpen();
    if (elapsedNanos < 0) throw new IllegalArgumentException("elapsedNanos must not be negative");
    Map<String, Long> metrics = MachineMetrics.capture(module).asMap();
    emit(descriptor, occurrence, "module", "<module>", elapsedNanos, modified,
        metrics, metrics, details);
  }

  private void emit(
      PassDescriptor descriptor,
      int occurrence,
      String targetKind,
      String targetName,
      long elapsedNanos,
      boolean modified,
      Map<String, Long> before,
      Map<String, Long> after,
      Map<String, Long> details) {
    sink.accept(new PassRemark(descriptor.id(), occurrence, descriptor.stage(), targetKind,
        targetName, elapsedNanos, modified, before, after, details,
        descriptor.decisionObservable()
            ? DecisionObservability.AVAILABLE
            : DecisionObservability.UNAVAILABLE));
  }

  private static void validateBackendDescriptor(
      PassDescriptor descriptor, int occurrence) {
    Objects.requireNonNull(descriptor, "descriptor");
    if (!descriptor.stage().isBackend()) {
      throw new IllegalArgumentException(
          "pass '" + descriptor.id() + "' is not a backend pass");
    }
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException(
          "invalid occurrence " + occurrence + " for " + descriptor.id());
    }
  }

  private static void validate(
      PassDescriptor descriptor, PassDescriptor.Stage stage, int occurrence) {
    Objects.requireNonNull(descriptor, "descriptor");
    if (descriptor.stage() != stage) {
      throw new IllegalArgumentException("pass '" + descriptor.id() + "' has stage "
          + descriptor.stage() + ", expected " + stage);
    }
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException("invalid occurrence " + occurrence + " for "
          + descriptor.id());
    }
  }

  private static Map<String, Long> prefixed(String prefix, Map<String, Long> source) {
    LinkedHashMap<String, Long> result = new LinkedHashMap<>();
    source.forEach((key, value) -> result.put(prefix + key, value));
    return Collections.unmodifiableMap(result);
  }

  @Override
  public void close() {
    if (!enabled) return;
    checkOwnerAndOpen();
    closed = true;
  }

  private void checkOwnerAndOpen() {
    if (!enabled) return;
    if (Thread.currentThread() != ownerThread) {
      throw new IllegalStateException(
          "backend instrumentation is single-threaded and owned by '"
              + ownerThread.getName() + "'");
    }
    if (closed) throw new IllegalStateException("backend instrumentation is closed");
  }
}
