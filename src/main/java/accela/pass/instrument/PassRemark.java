package accela.pass.instrument;

import accela.pass.PassDescriptor;
import java.util.LinkedHashMap;
import java.util.Collections;
import java.util.Map;
import java.util.Objects;

/** Machine-readable observation for one scheduled pass invocation. */
public record PassRemark(
    String passId,
    int occurrence,
    PassDescriptor.Stage stage,
    String targetKind,
    String targetName,
    long elapsedNanos,
    boolean changed,
    Map<String, Long> before,
    Map<String, Long> after,
    Map<String, Long> details,
    DecisionObservability decisionObservability) implements OptimizationRemark {

  public PassRemark {
    Objects.requireNonNull(passId, "passId");
    Objects.requireNonNull(stage, "stage");
    Objects.requireNonNull(targetKind, "targetKind");
    Objects.requireNonNull(targetName, "targetName");
    Objects.requireNonNull(decisionObservability, "decisionObservability");
    if (occurrence < 1) throw new IllegalArgumentException("occurrence must be positive");
    if (elapsedNanos < 0) throw new IllegalArgumentException("elapsedNanos must not be negative");
    before = immutableLongMap(before, "before");
    after = immutableLongMap(after, "after");
    details = immutableLongMap(details, "details");
  }

  public Map<String, Long> delta() {
    LinkedHashMap<String, Long> delta = new LinkedHashMap<>();
    LinkedHashMap<String, Long> keys = new LinkedHashMap<>(before);
    after.forEach(keys::putIfAbsent);
    for (String key : keys.keySet()) {
      long difference = after.getOrDefault(key, 0L) - before.getOrDefault(key, 0L);
      if (difference != 0) delta.put(key, difference);
    }
    return Collections.unmodifiableMap(delta);
  }

  private static Map<String, Long> immutableLongMap(Map<String, Long> source, String name) {
    Objects.requireNonNull(source, name);
    LinkedHashMap<String, Long> copy = new LinkedHashMap<>();
    source.forEach(
        (key, value) -> {
          if (key == null || key.isBlank()) {
            throw new IllegalArgumentException(name + " contains a blank key");
          }
          copy.put(key, Objects.requireNonNull(value, name + "[" + key + "]"));
        });
    return Collections.unmodifiableMap(copy);
  }
}
