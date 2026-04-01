package accela.pass.ir;

import accela.pass.PreservedAnalyses;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Lazily computes and caches module-local analysis results.
 */
public final class ModuleAnalysisManager {
  private final Map<Class<?>, ModuleAnalysis<?>> analyses = new LinkedHashMap<>();
  private final Map<accela.ir.Module, Map<Class<?>, Object>> cache = new IdentityHashMap<>();

  /** Registers a module analysis implementation under its analysis type key. */
  public <T> void registerPass(Class<? extends ModuleAnalysis<T>> analysisType,
                               ModuleAnalysis<T> analysis) {
    analyses.put(analysisType, analysis);
  }

  /** Returns a cached analysis result, computing it on first use. */
  @SuppressWarnings("unchecked")
  public <T> T getResult(Class<? extends ModuleAnalysis<T>> analysisType, accela.ir.Module module) {
    ModuleAnalysis<T> analysis = (ModuleAnalysis<T>) analyses.get(analysisType);
    if (analysis == null) {
      throw new IllegalStateException("Unregistered module analysis: " + analysisType.getName());
    }
    Map<Class<?>, Object> moduleCache = cache.computeIfAbsent(module, ignored -> new LinkedHashMap<>());
    if (!moduleCache.containsKey(analysisType)) {
      moduleCache.put(analysisType, analysis.run(module, this));
    }
    return (T) moduleCache.get(analysisType);
  }

  /** Invalidates cached results that are not preserved for the given module. */
  public void invalidate(accela.ir.Module module, PreservedAnalyses pa) {
    if (pa.preservesAll()) return;
    Map<Class<?>, Object> moduleCache = cache.get(module);
    if (moduleCache == null) return;
    if (pa.preservesNone()) {
      moduleCache.clear();
      return;
    }
    moduleCache.entrySet().removeIf(entry -> !pa.isPreserved(entry.getKey()));
  }
}
