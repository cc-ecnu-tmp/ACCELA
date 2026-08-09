package accela.pass;

import java.util.Objects;

/** Stable identity and scheduling contract for one optimization or lowering stage. */
public record PassDescriptor(
    String id,
    String logicalFamilyId,
    String displayName,
    Stage stage,
    int fullPipelineOccurrences,
    boolean required,
    boolean decisionObservable) {

  public enum Stage {
    IR_FUNCTION,
    IR_MODULE,
    BACKEND_FUNCTION,
    BACKEND_MODULE;

    public boolean isIr() {
      return this == IR_FUNCTION || this == IR_MODULE;
    }

    public boolean isBackend() {
      return !isIr();
    }
  }

  public PassDescriptor {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(logicalFamilyId, "logicalFamilyId");
    Objects.requireNonNull(displayName, "displayName");
    Objects.requireNonNull(stage, "stage");
    if (!id.matches("[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")) {
      throw new IllegalArgumentException("invalid pass id: " + id);
    }
    if (!logicalFamilyId.matches("[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")) {
      throw new IllegalArgumentException("invalid logical family id: " + logicalFamilyId);
    }
    if (displayName.isBlank()) throw new IllegalArgumentException("displayName must not be blank");
    if (fullPipelineOccurrences < 1) {
      throw new IllegalArgumentException("fullPipelineOccurrences must be positive for " + id);
    }
  }
}
