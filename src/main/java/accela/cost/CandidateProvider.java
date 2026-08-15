package accela.cost;

import java.util.List;

public interface CandidateProvider<T> {
  List<? extends OptimizationCandidate<T>> candidates(T input);
}
