package accela.pass.ir.instrument;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.PassRemark;
import accela.pass.instrument.PassRemarkSink;
import accela.pass.instrument.DecisionObservability;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.verify.IRVerifier;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Shared IR verification, timing, and structural-change instrumentation. */
public final class PassInstrumentation implements AutoCloseable {
  public static final class PassReport {
    public final String passName;
    public final String passId;
    public final int occurrence;
    public final String targetKind;
    public final String targetName;
    public final long elapsedNanos;
    public final Map<String, Long> beforeMetrics;
    public final Map<String, Long> afterMetrics;
    public final String metricsSummary;
    public final PreservedAnalyses preservedAnalyses;

    private PassReport(
        String passName,
        String passId,
        int occurrence,
        String targetKind,
        String targetName,
        long elapsedNanos,
        Map<String, Long> beforeMetrics,
        Map<String, Long> afterMetrics,
        String metricsSummary,
        PreservedAnalyses preservedAnalyses) {
      this.passName = passName;
      this.passId = passId;
      this.occurrence = occurrence;
      this.targetKind = targetKind;
      this.targetName = targetName;
      this.elapsedNanos = elapsedNanos;
      this.beforeMetrics = beforeMetrics;
      this.afterMetrics = afterMetrics;
      this.metricsSummary = metricsSummary;
      this.preservedAnalyses = preservedAnalyses;
    }

    public String format() {
      return "[pass] " + passName + " on " + targetKind + " " + targetName + ": "
          + metricsSummary;
    }
  }

  private static final class ActiveRun {
    final String passName;
    final PassDescriptor descriptor;
    final int occurrence;
    final String targetKind;
    final String targetName;
    final IRMetrics beforeMetrics;
    final long startedNanos;

    ActiveRun(
        String passName,
        PassDescriptor descriptor,
        int occurrence,
        String targetKind,
        String targetName,
        IRMetrics beforeMetrics) {
      this.passName = passName;
      this.descriptor = descriptor;
      this.occurrence = occurrence;
      this.targetKind = targetKind;
      this.targetName = targetName;
      this.beforeMetrics = beforeMetrics;
      this.startedNanos = System.nanoTime();
    }
  }

  private static final PassInstrumentation NOOP =
      new PassInstrumentation(false, false, PassRemarkSink.noop());

  private final boolean enabled;
  private final boolean printReports;
  private final PassRemarkSink remarkSink;
  private final Thread ownerThread;
  private final Deque<ActiveRun> activeRuns = new ArrayDeque<>();
  private final List<PassReport> reports = new ArrayList<>();
  private boolean closed;

  public static PassInstrumentation noop() {
    return NOOP;
  }

  public static PassInstrumentation enabled(boolean printReports) {
    return new PassInstrumentation(true, printReports, PassRemarkSink.noop());
  }

  public static PassInstrumentation observed(
      boolean printReports, PassRemarkSink remarkSink) {
    return new PassInstrumentation(true, printReports, Objects.requireNonNull(remarkSink, "remarkSink"));
  }

  /** Returns whether verification, metrics, timing, and remark observation are active. */
  public boolean isEnabled() {
    return enabled;
  }

  private PassInstrumentation(
      boolean enabled, boolean printReports, PassRemarkSink remarkSink) {
    this.enabled = enabled;
    this.printReports = printReports;
    this.remarkSink = remarkSink;
    this.ownerThread = enabled ? Thread.currentThread() : null;
  }

  public List<PassReport> getReports() {
    checkOwnerAndOpen();
    return List.copyOf(reports);
  }

  /** Creates a fail-fast candidate decision emitter bound to one scheduled pass and target. */
  public PassDecisionEmitter decisionEmitter(
      PassDescriptor descriptor, int occurrence, String targetKind, String targetName) {
    checkOwnerAndOpen();
    if (!enabled) {
      throw new IllegalStateException("pass decision observation is disabled");
    }
    if (!descriptor.decisionObservable()) {
      throw new IllegalArgumentException(
          "pass '" + descriptor.id() + "' is not registered as decision-observable");
    }
    return remarkSink.decisionEmitter(descriptor, occurrence, targetKind, targetName);
  }

  public void beforeFunctionPass(Object pass, Function function) {
    beforeFunctionPass(null, 1, pass, function);
  }

  public void beforeFunctionPass(
      PassDescriptor descriptor, int occurrence, Object pass, Function function) {
    if (!enabled) return;
    checkOwnerAndOpen();
    validateDescriptor(descriptor, PassDescriptor.Stage.IR_FUNCTION, occurrence);
    IRVerifier.verifyFunction(function);
    activeRuns.push(new ActiveRun(passName(pass), descriptor, occurrence, "function",
        "@" + function.getName(), IRMetrics.capture(function)));
  }

  public void afterFunctionPass(
      Object pass, Function function, PreservedAnalyses preservedAnalyses) {
    afterFunctionPass(null, 1, pass, function, preservedAnalyses.isModified(), preservedAnalyses);
  }

  public void afterFunctionPass(
      PassDescriptor descriptor,
      int occurrence,
      Object pass,
      Function function,
      boolean modified,
      PreservedAnalyses preservedAnalyses) {
    if (!enabled) return;
    checkOwnerAndOpen();
    long endedNanos = System.nanoTime();
    IRVerifier.verifyFunction(function);
    finish(descriptor, occurrence, pass, "function", "@" + function.getName(),
        IRMetrics.capture(function), modified, preservedAnalyses, endedNanos);
  }

  public void beforeModulePass(Object pass, accela.ir.Module module) {
    beforeModulePass(null, 1, pass, module);
  }

  public void beforeModulePass(
      PassDescriptor descriptor, int occurrence, Object pass, accela.ir.Module module) {
    if (!enabled) return;
    checkOwnerAndOpen();
    validateDescriptor(descriptor, PassDescriptor.Stage.IR_MODULE, occurrence);
    IRVerifier.verifyModule(module);
    activeRuns.push(new ActiveRun(passName(pass), descriptor, occurrence, "module", "<module>",
        IRMetrics.capture(module)));
  }

  public void afterModulePass(
      Object pass, accela.ir.Module module, PreservedAnalyses preservedAnalyses) {
    afterModulePass(null, 1, pass, module, preservedAnalyses.isModified(), preservedAnalyses);
  }

  public void afterModulePass(
      PassDescriptor descriptor,
      int occurrence,
      Object pass,
      accela.ir.Module module,
      boolean modified,
      PreservedAnalyses preservedAnalyses) {
    if (!enabled) return;
    checkOwnerAndOpen();
    long endedNanos = System.nanoTime();
    IRVerifier.verifyModule(module);
    finish(descriptor, occurrence, pass, "module", "<module>", IRMetrics.capture(module),
        modified, preservedAnalyses, endedNanos);
  }

  private void finish(
      PassDescriptor descriptor,
      int occurrence,
      Object pass,
      String targetKind,
      String targetName,
      IRMetrics afterMetrics,
      boolean modified,
      PreservedAnalyses preservedAnalyses,
      long endedNanos) {
    if (activeRuns.isEmpty()) {
      throw new IllegalStateException("pass instrumentation completed without a matching start");
    }
    ActiveRun run = activeRuns.pop();
    String passName = passName(pass);
    String expectedId = descriptor == null ? passName : descriptor.id();
    String runId = run.descriptor == null ? run.passName : run.descriptor.id();
    if (!run.passName.equals(passName) || !runId.equals(expectedId)
        || run.occurrence != occurrence || !run.targetKind.equals(targetKind)
        || !run.targetName.equals(targetName)) {
      throw new IllegalStateException("mismatched pass instrumentation nesting for " + expectedId);
    }

    long elapsedNanos = Math.max(0L, endedNanos - run.startedNanos);
    Map<String, Long> before = run.beforeMetrics.asMap();
    Map<String, Long> after = afterMetrics.asMap();
    PassReport report = new PassReport(passName, expectedId, occurrence, targetKind, targetName,
        elapsedNanos, before, after, run.beforeMetrics.diffSummary(afterMetrics), preservedAnalyses);
    reports.add(report);
    if (descriptor != null) {
      remarkSink.accept(new PassRemark(descriptor.id(), occurrence, descriptor.stage(), targetKind,
          targetName, elapsedNanos, modified, before, after, Map.of(), descriptor.decisionObservable()
              ? DecisionObservability.AVAILABLE : DecisionObservability.UNAVAILABLE));
    }
    if (printReports) System.err.println(report.format());
  }

  private static void validateDescriptor(
      PassDescriptor descriptor, PassDescriptor.Stage expectedStage, int occurrence) {
    if (descriptor == null) return;
    if (descriptor.stage() != expectedStage) {
      throw new IllegalArgumentException(
          "pass '" + descriptor.id() + "' has stage " + descriptor.stage()
              + ", expected " + expectedStage);
    }
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException("invalid occurrence " + occurrence + " for " + descriptor.id());
    }
  }

  private static String passName(Object pass) {
    String name = pass.getClass().getName();
    int pkg = name.lastIndexOf('.');
    if (pkg >= 0) name = name.substring(pkg + 1);
    return name.replace('$', '.');
  }

  /** Verifies that all scheduled pass observations were paired and closes this owner session. */
  @Override
  public void close() {
    if (!enabled) return;
    checkOwnerAndOpen();
    closed = true;
    if (!activeRuns.isEmpty()) {
      ActiveRun run = activeRuns.peek();
      throw new IllegalStateException(
          "unpaired pass instrumentation start for "
              + (run.descriptor == null ? run.passName : run.descriptor.id())
              + " on " + run.targetName);
    }
  }

  private void checkOwnerAndOpen() {
    if (!enabled) return;
    if (Thread.currentThread() != ownerThread) {
      throw new IllegalStateException(
          "pass instrumentation is single-threaded and owned by '" + ownerThread.getName() + "'");
    }
    if (closed) throw new IllegalStateException("pass instrumentation is closed");
  }
}
