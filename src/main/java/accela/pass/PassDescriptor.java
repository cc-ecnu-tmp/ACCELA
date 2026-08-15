package accela.pass;

import java.util.LinkedHashSet;
import java.util.List;

/** Stable pass identity, phase boundary, dependency and legality contract. */
public record PassDescriptor(
    String id,
    Stage stage,
    boolean required,
    List<String> dependencies,
    List<String> legalityObligations) {
  public enum Stage { IR, LOWERING, MIR, REGISTER_ALLOCATION, EMISSION }

  public PassDescriptor {
    if (id == null || !id.matches("[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")) {
      throw new IllegalArgumentException("invalid pass id: " + id);
    }
    if (stage == null) throw new IllegalArgumentException("pass stage is required");
    dependencies = validatedIds(dependencies, "dependency");
    legalityObligations = validatedIds(legalityObligations, "legality obligation");
    if (required && !legalityObligations.isEmpty()) {
      throw new IllegalArgumentException("required passes do not have profitability obligations");
    }
    if (!required && legalityObligations.isEmpty()) {
      throw new IllegalArgumentException("optional pass requires a legality obligation");
    }
    for (String obligation : legalityObligations) {
      if (!obligation.startsWith(id + ".")) {
        throw new IllegalArgumentException("legality obligation is not scoped to pass " + id);
      }
    }
  }

  public String primaryObligation() {
    if (legalityObligations.isEmpty()) {
      throw new IllegalStateException("required pass has no legality obligation: " + id);
    }
    return legalityObligations.getFirst();
  }

  private static List<String> validatedIds(List<String> values, String name) {
    if (values == null) throw new IllegalArgumentException(name + " list is required");
    LinkedHashSet<String> result = new LinkedHashSet<>();
    for (String value : values) {
      if (value == null || !value.matches("[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")) {
        throw new IllegalArgumentException("invalid " + name + ": " + value);
      }
      if (!result.add(value)) throw new IllegalArgumentException("duplicate " + name + ": " + value);
    }
    return List.copyOf(result);
  }
}
