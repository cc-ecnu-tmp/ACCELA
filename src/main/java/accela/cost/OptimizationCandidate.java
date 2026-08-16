package accela.cost;

import java.util.Map;

/** Candidate contract deliberately contains no source path, source bytes, case id, or output. */
public interface OptimizationCandidate<T> {
  String id();
  Map<String, String> parameters();
  LegalityResult legality(T input);
  T apply(T snapshot);
}
