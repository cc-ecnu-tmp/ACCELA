package accela.cost;

import accela.backend.regalloc.AllocationEstimate;
import accela.util.StrictJson;
import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/** DecisionTrace v1 JSONL writer. Paths belong to the sink, never to cost-model inputs. */
public interface DecisionTraceSink extends AutoCloseable {
  void accept(Decision decision);

  @Override
  default void close() {}

  static DecisionTraceSink noop() { return decision -> {}; }

  static DecisionTraceSink jsonl(Path path) throws IOException {
    BufferedWriter writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
    return new DecisionTraceSink() {
      @Override
      public synchronized void accept(Decision decision) {
        try {
          writer.write(StrictJson.stringify(decision.toJson()));
          writer.newLine();
          writer.flush();
        } catch (IOException exception) {
          throw new IllegalStateException("failed to write cost decision trace", exception);
        }
      }

      @Override
      public synchronized void close() {
        try {
          writer.close();
        } catch (IOException exception) {
          throw new IllegalStateException("failed to close cost decision trace", exception);
        }
      }
    };
  }

  record Decision(
      String profile,
      String candidate,
      String targetKind,
      String targetName,
      String status,
      String reason,
      Map<String, String> parameters,
      CostEstimate baseline,
      CostEstimate transformed,
      AllocationEstimate allocation,
      int expansions,
      int expansionBudget) {

    Map<String, Object> toJson() {
      LinkedHashMap<String, Object> root = new LinkedHashMap<>();
      root.put("schema_version", "decision-trace.v1");
      root.put("profile", profile);
      root.put("candidate", candidate);
      root.put("target_kind", targetKind);
      root.put("target_name", targetName);
      root.put("status", status);
      root.put("reason", reason);
      root.put("parameters", parameters);
      root.put("baseline", costJson(baseline));
      root.put("transformed", costJson(transformed));
      root.put("allocation", allocation == null ? null : Map.of(
          "predicted_spills", allocation.predictedSpills(),
          "spill_weight", allocation.spillWeight(),
          "max_integer_live", allocation.maxIntegerLive(),
          "max_float_live", allocation.maxFloatLive(),
          "coalescing_loss", allocation.coalescingLoss()));
      root.put("expansions", expansions);
      root.put("expansion_budget", expansionBudget);
      return root;
    }

    private static Map<String, Object> costJson(CostEstimate cost) {
      if (cost == null) return null;
      LinkedHashMap<String, Object> result = new LinkedHashMap<>();
      result.put("cycles", cost.cycles());
      result.put("uncertainty", cost.uncertainty());
      result.put("critical_path", cost.criticalPath());
      result.put("frontend", cost.frontend());
      result.put("resources", cost.resources());
      result.put("memory", cost.memory());
      result.put("branch", cost.branch());
      result.put("spill", cost.spill());
      result.put("code_size", cost.codeSize());
      return result;
    }
  }
}
