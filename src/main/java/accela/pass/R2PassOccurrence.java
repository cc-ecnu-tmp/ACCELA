package accela.pass;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** One stable occurrence of a production pass in the R2 scheduling DAG. */
public record R2PassOccurrence(
    String id,
    String familyId,
    PassDescriptor.Stage stage,
    Scope scope,
    boolean required,
    List<String> dependencies,
    Set<Analysis> requiredAnalyses,
    Set<Analysis> invalidatedAnalyses) {
  public enum Scope { FUNCTION, MODULE }

  public enum Analysis {
    DOMINATOR_TREE,
    POST_DOMINATOR_TREE,
    LOOP_INFO,
    INDUCTION_VARIABLES,
    SCALAR_EVOLUTION,
    CALL_GRAPH,
    LIVENESS,
    INTERFERENCE
  }

  public R2PassOccurrence {
    validateId(id, "occurrence id");
    validateId(familyId, "family id");
    if (stage == null) throw new IllegalArgumentException("R2 pass stage is required");
    if (scope == null) throw new IllegalArgumentException("R2 pass scope is required");
    dependencies = uniqueIds(dependencies);
    requiredAnalyses = immutableAnalyses(requiredAnalyses, "required analyses");
    invalidatedAnalyses = immutableAnalyses(invalidatedAnalyses, "invalidated analyses");
    if (stage == PassDescriptor.Stage.IR
        && requiredAnalyses.stream().anyMatch(R2PassOccurrence::isMachineAnalysis)) {
      throw new IllegalArgumentException("IR pass cannot require machine analysis: " + id);
    }
    if (stage.ordinal() >= PassDescriptor.Stage.MIR.ordinal()
        && requiredAnalyses.stream().anyMatch(R2PassOccurrence::isIrAnalysis)) {
      throw new IllegalArgumentException("machine pass cannot require IR analysis: " + id);
    }
  }

  public String legalityObligation() { return id + ".production-semantics"; }

  private static boolean isIrAnalysis(Analysis analysis) {
    return switch (analysis) {
      case DOMINATOR_TREE, POST_DOMINATOR_TREE, LOOP_INFO, INDUCTION_VARIABLES,
          SCALAR_EVOLUTION, CALL_GRAPH -> true;
      case LIVENESS, INTERFERENCE -> false;
    };
  }

  private static boolean isMachineAnalysis(Analysis analysis) {
    return analysis == Analysis.LIVENESS || analysis == Analysis.INTERFERENCE;
  }

  private static void validateId(String value, String name) {
    if (value == null || !value.matches("[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")) {
      throw new IllegalArgumentException("invalid " + name + ": " + value);
    }
  }

  private static List<String> uniqueIds(List<String> values) {
    if (values == null) throw new IllegalArgumentException("dependency list is required");
    LinkedHashSet<String> result = new LinkedHashSet<>();
    for (String value : values) {
      validateId(value, "dependency");
      if (!result.add(value)) throw new IllegalArgumentException("duplicate dependency: " + value);
    }
    return List.copyOf(result);
  }

  private static Set<Analysis> immutableAnalyses(Set<Analysis> values, String name) {
    if (values == null || values.stream().anyMatch(java.util.Objects::isNull)) {
      throw new IllegalArgumentException(name + " are required");
    }
    return Set.copyOf(values);
  }
}
