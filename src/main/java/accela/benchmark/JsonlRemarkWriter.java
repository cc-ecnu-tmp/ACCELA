package accela.benchmark;

import accela.pass.instrument.PassRemark;
import accela.pass.instrument.PassRemarkSink;
import accela.pass.instrument.DecisionRemark;
import accela.pass.instrument.OptimizationRemark;
import accela.util.StrictJson;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Locale;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.Objects;

/** UTF-8 JSON Lines sink for benchmark pass remarks. */
public final class JsonlRemarkWriter implements PassRemarkSink, AutoCloseable {
  private final BufferedWriter writer;
  private long sequence;
  private final Set<DecisionKey> unterminatedCandidates = new LinkedHashSet<>();
  private boolean closed;

  public JsonlRemarkWriter(Path path) throws IOException {
    Objects.requireNonNull(path, "path");
    writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8,
        StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING,
        StandardOpenOption.WRITE);
  }

  @Override
  public synchronized void accept(OptimizationRemark remark) {
    Objects.requireNonNull(remark, "remark");
    if (closed) throw new IllegalStateException("remark writer is closed");
    DecisionKey decisionKey = remark instanceof DecisionRemark decision
        ? DecisionKey.from(decision) : null;
    if (remark instanceof DecisionRemark decision) {
      if (decision.decision() == accela.pass.instrument.DecisionStatus.CANDIDATE) {
        if (unterminatedCandidates.contains(decisionKey)) {
          throw new IllegalStateException("optimization candidate is already open: " + decisionKey);
        }
      } else if (!unterminatedCandidates.contains(decisionKey)) {
        throw new IllegalStateException(
            "terminal optimization decision has no matching open candidate: " + decisionKey);
      }
    }
    LinkedHashMap<String, Object> json = new LinkedHashMap<>();
    json.put("schema_version", "optimization-remark.v1");
    json.put("sequence", ++sequence);
    json.put("event_type", remark instanceof PassRemark ? "pass_summary" : "decision");
    json.put("pass", remark.passId());
    json.put("occurrence", remark.occurrence());
    json.put("stage", remark.stage().name().toLowerCase(Locale.ROOT));
    json.put("target_kind", remark.targetKind());
    json.put("target_name", remark.targetName());
    if (remark instanceof PassRemark summary) {
      json.put("elapsed_ns", summary.elapsedNanos());
      json.put("changed", summary.changed());
      json.put("before", summary.before());
      json.put("after", summary.after());
      json.put("delta", summary.delta());
      json.put("details", summary.details());
      json.put("decision_observability",
          summary.decisionObservability().name().toLowerCase(Locale.ROOT));
    } else if (remark instanceof DecisionRemark decision) {
      json.put("decision", decision.decision().name().toLowerCase(Locale.ROOT));
      json.put("reason", decision.reason().name().toLowerCase(Locale.ROOT));
    } else {
      throw new IllegalArgumentException(
          "unsupported optimization remark type: " + remark.getClass().getName());
    }
    try {
      writer.write(StrictJson.stringify(json));
      writer.newLine();
      writer.flush();
      if (remark instanceof DecisionRemark decision) {
        switch (decision.decision()) {
          case CANDIDATE -> unterminatedCandidates.add(decisionKey);
          case APPLIED, REJECTED -> unterminatedCandidates.remove(decisionKey);
        }
      }
    } catch (IOException exception) {
      throw new UncheckedIOException("failed to write pass remark", exception);
    }
  }

  @Override
  public synchronized void close() throws IOException {
    if (closed) return;
    closed = true;
    IOException closeFailure = null;
    try {
      writer.close();
    } catch (IOException exception) {
      closeFailure = exception;
    }
    if (!unterminatedCandidates.isEmpty()) {
      IllegalStateException exception = new IllegalStateException(
          "remark stream closed with " + unterminatedCandidates.size()
              + " unterminated optimization candidate(s)");
      if (closeFailure != null) exception.addSuppressed(closeFailure);
      throw exception;
    }
    if (closeFailure != null) throw closeFailure;
  }

  private record DecisionKey(
      String passId,
      int occurrence,
      accela.pass.PassDescriptor.Stage stage,
      String targetKind,
      String targetName) {
    static DecisionKey from(DecisionRemark decision) {
      return new DecisionKey(
          decision.passId(),
          decision.occurrence(),
          decision.stage(),
          decision.targetKind(),
          decision.targetName());
    }
  }
}
