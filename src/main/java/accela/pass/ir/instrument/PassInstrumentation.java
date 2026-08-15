package accela.pass.ir.instrument;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import accela.pass.ir.verify.IRVerifier;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/**
 * Shared pass instrumentation for verification and quantitative change reporting.
 */
public final class PassInstrumentation {
  public static final class PassReport {
    public final String passName;
    public final String targetKind;
    public final String targetName;
    public final String metricsSummary;
    public final PreservedAnalyses preservedAnalyses;

    private PassReport(
        String passName,
        String targetKind,
        String targetName,
        String metricsSummary,
        PreservedAnalyses preservedAnalyses) {
      this.passName = passName;
      this.targetKind = targetKind;
      this.targetName = targetName;
      this.metricsSummary = metricsSummary;
      this.preservedAnalyses = preservedAnalyses;
    }

    public String format() {
      return "[pass] " + passName + " on " + targetKind + " " + targetName + ": " + metricsSummary;
    }
  }

  private static final class ActiveRun {
    final String passName;
    final String targetKind;
    final String targetName;
    final IRMetrics beforeMetrics;

    ActiveRun(String passName, String targetKind, String targetName, IRMetrics beforeMetrics) {
      this.passName = passName;
      this.targetKind = targetKind;
      this.targetName = targetName;
      this.beforeMetrics = beforeMetrics;
    }
  }

  private static final PassInstrumentation NOOP = new PassInstrumentation(false, false);

  private final boolean enabled;
  private final boolean printReports;
  private final Deque<ActiveRun> activeRuns = new ArrayDeque<>();
  private final List<PassReport> reports = new ArrayList<>();

  public static PassInstrumentation noop() {
    return NOOP;
  }

  public static PassInstrumentation enabled(boolean printReports) {
    return new PassInstrumentation(true, printReports);
  }

  private PassInstrumentation(boolean enabled, boolean printReports) {
    this.enabled = enabled;
    this.printReports = printReports;
  }

  public List<PassReport> getReports() {
    return List.copyOf(reports);
  }

  public void beforeFunctionPass(Object pass, Function function) {
    if (!enabled) return;
    IRVerifier.verifyFunction(function);
    activeRuns.push(
        new ActiveRun(passName(pass), "function", "@" + function.getName(), IRMetrics.capture(function)));
  }

  public void afterFunctionPass(Object pass, Function function, PreservedAnalyses preservedAnalyses) {
    if (!enabled) return;
    IRVerifier.verifyFunction(function);
    finish(pass, "function", "@" + function.getName(), IRMetrics.capture(function), preservedAnalyses);
  }

  public void beforeModulePass(Object pass, accela.ir.Module module) {
    if (!enabled) return;
    IRVerifier.verifyModule(module);
    activeRuns.push(new ActiveRun(passName(pass), "module", "<module>", IRMetrics.capture(module)));
  }

  public void afterModulePass(
      Object pass, accela.ir.Module module, PreservedAnalyses preservedAnalyses) {
    if (!enabled) return;
    IRVerifier.verifyModule(module);
    finish(pass, "module", "<module>", IRMetrics.capture(module), preservedAnalyses);
  }

  private void finish(
      Object pass,
      String targetKind,
      String targetName,
      IRMetrics afterMetrics,
      PreservedAnalyses preservedAnalyses) {
    ActiveRun run = activeRuns.pop();
    String passName = passName(pass);
    if (!run.passName.equals(passName)
        || !run.targetKind.equals(targetKind)
        || !run.targetName.equals(targetName)) {
      throw new IllegalStateException("Mismatched pass instrumentation nesting for " + passName);
    }

    PassReport report =
        new PassReport(passName, targetKind, targetName, run.beforeMetrics.diffSummary(afterMetrics),
            preservedAnalyses);
    reports.add(report);
    if (printReports) {
      System.err.println(report.format());
    }
  }

  private static String passName(Object pass) {
    String name = pass.getClass().getName();
    int pkg = name.lastIndexOf('.');
    if (pkg >= 0) name = name.substring(pkg + 1);
    return name.replace('$', '.');
  }
}
