package accela.pass.ir;

import accela.ir.Function;
import accela.pass.PreservedAnalyses;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Lazily computes and caches function-local analysis results.
 */
public final class FunctionAnalysisManager {
  private final Map<Class<?>, FunctionAnalysis<?>> analyses = new LinkedHashMap<>();
  private final Map<Function, Map<Class<?>, Object>> cache = new IdentityHashMap<>();

  /** Registers a function analysis implementation under its result type key. */
  public <T> void registerPass(Class<? extends FunctionAnalysis<T>> analysisType,
                               FunctionAnalysis<T> analysis) {
    analyses.put(analysisType, analysis);
  }

  /** Returns a cached analysis result, computing it on first use. */
  @SuppressWarnings("unchecked")
  public <T> T getResult(Class<? extends FunctionAnalysis<T>> analysisType, Function function) {
    FunctionAnalysis<T> analysis = (FunctionAnalysis<T>) analyses.get(analysisType);
    if (analysis == null) {
      throw new IllegalStateException("Unregistered function analysis: " + analysisType.getName());
    }
    Map<Class<?>, Object> functionCache = cache.computeIfAbsent(function, ignored -> new LinkedHashMap<>());
    if (!functionCache.containsKey(analysisType)) {
      functionCache.put(analysisType, analysis.run(function, this));
    }
    return (T) functionCache.get(analysisType);
  }

  /** Invalidates cached results that are not preserved for the given function. */
  public void invalidate(Function function, PreservedAnalyses pa) {
    if (pa.preservesAll()) return;
    Map<Class<?>, Object> functionCache = cache.get(function);
    if (functionCache == null) return;
    if (pa.preservesNone()) {
      functionCache.clear();
      return;
    }
    functionCache.entrySet().removeIf(entry -> !pa.isPreserved(entry.getKey()));
  }

  /** Conservatively invalidates every cached function-analysis result in this manager. */
  public void invalidateAll() {
    cache.clear();
  }
}
