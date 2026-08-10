package accela.pass;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;

/** Stable identity and scheduling contract for one production or candidate pipeline stage. */
public record PassDescriptor(
    String id,
    String logicalFamilyId,
    String displayName,
    Stage stage,
    int fullPipelineOccurrences,
    Lifecycle lifecycle,
    boolean decisionObservable,
    CandidateAnchor candidateAnchor,
    List<String> legalityObligationIds) {

  /** Matches pass-registry.v2 display_name maxLength (Unicode code points). */
  public static final int MAX_DISPLAY_NAME_CODE_POINTS = 256;

  public enum Lifecycle {
    REQUIRED,
    PRODUCTION,
    CANDIDATE
  }

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

  public enum AnchorPosition {
    BEFORE,
    AFTER
  }

  /** Stable insertion point for a default-off candidate in the production pipeline layout. */
  public record CandidateAnchor(
      String passId,
      int occurrence,
      AnchorPosition position) {

    public CandidateAnchor {
      Objects.requireNonNull(passId, "passId");
      Objects.requireNonNull(position, "position");
      requireId(passId, "candidate anchor pass id");
      if (occurrence < 1) {
        throw new IllegalArgumentException("candidate anchor occurrence must be positive");
      }
    }
  }

  public PassDescriptor {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(logicalFamilyId, "logicalFamilyId");
    Objects.requireNonNull(displayName, "displayName");
    Objects.requireNonNull(stage, "stage");
    Objects.requireNonNull(lifecycle, "lifecycle");
    Objects.requireNonNull(legalityObligationIds, "legalityObligationIds");
    requireId(id, "pass id");
    requireId(logicalFamilyId, "logical family id");
    if (displayName.isBlank()) throw new IllegalArgumentException("displayName must not be blank");
    requireWellFormedUtf16(displayName, "displayName");
    if (displayName.codePointCount(0, displayName.length()) > MAX_DISPLAY_NAME_CODE_POINTS) {
      throw new IllegalArgumentException(
          "displayName exceeds " + MAX_DISPLAY_NAME_CODE_POINTS + " Unicode code points");
    }
    if (fullPipelineOccurrences < 1) {
      throw new IllegalArgumentException("fullPipelineOccurrences must be positive for " + id);
    }
    LinkedHashSet<String> obligations = new LinkedHashSet<>();
    for (String obligation : legalityObligationIds) {
      Objects.requireNonNull(obligation, "legality obligation id");
      requireId(obligation, "legality obligation id");
      if (!obligation.startsWith(id + ".")) {
        throw new IllegalArgumentException(
            "legality obligation '" + obligation + "' is not scoped to candidate '" + id + "'");
      }
      if (!obligations.add(obligation)) {
        throw new IllegalArgumentException("duplicate legality obligation id: " + obligation);
      }
    }
    legalityObligationIds = List.copyOf(obligations);

    if (lifecycle == Lifecycle.CANDIDATE) {
      if (stage == Stage.BACKEND_MODULE) {
        throw new IllegalArgumentException(
            "backend-module candidates are unsupported because module stages cross distinct "
                + "IR, machine-module, and assembly contracts: " + id);
      }
      if (!id.startsWith("candidate.")) {
        throw new IllegalArgumentException("candidate pass id must start with 'candidate.': " + id);
      }
      if (!logicalFamilyId.startsWith("candidate.")) {
        throw new IllegalArgumentException(
            "candidate logical family id must start with 'candidate.': " + logicalFamilyId);
      }
      if (!decisionObservable) {
        throw new IllegalArgumentException("candidate pass must be decision-observable: " + id);
      }
      if (candidateAnchor == null) {
        throw new IllegalArgumentException("candidate pass requires a stable pipeline anchor: " + id);
      }
      if (fullPipelineOccurrences != 1) {
        throw new IllegalArgumentException("candidate pass must have exactly one scheduled occurrence: " + id);
      }
      if (legalityObligationIds.isEmpty()) {
        throw new IllegalArgumentException("candidate pass requires legality obligations: " + id);
      }
    } else {
      if (id.startsWith("candidate.") || logicalFamilyId.startsWith("candidate.")) {
        throw new IllegalArgumentException(
            "non-candidate pass and family ids must not use the candidate namespace: " + id);
      }
      if (candidateAnchor != null || !legalityObligationIds.isEmpty()) {
        throw new IllegalArgumentException(
            "only candidate passes may declare an anchor or legality obligations: " + id);
      }
    }
  }

  public boolean required() {
    return lifecycle == Lifecycle.REQUIRED;
  }

  public boolean candidate() {
    return lifecycle == Lifecycle.CANDIDATE;
  }

  public boolean defaultEnabled() {
    return lifecycle != Lifecycle.CANDIDATE;
  }

  public void requireLegalityObligation(String obligationId) {
    Objects.requireNonNull(obligationId, "obligationId");
    if (!candidate()) {
      throw new IllegalStateException("non-candidate pass has no legality obligation contract: " + id);
    }
    if (!legalityObligationIds.contains(obligationId)) {
      throw new IllegalArgumentException(
          "unknown legality obligation '" + obligationId + "' for candidate '" + id + "'");
    }
  }

  private static void requireId(String value, String name) {
    if (!value.matches("[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*")) {
      throw new IllegalArgumentException("invalid " + name + ": " + value);
    }
  }

  private static void requireWellFormedUtf16(String value, String name) {
    for (int index = 0; index < value.length(); index++) {
      char current = value.charAt(index);
      if (Character.isHighSurrogate(current)) {
        if (index + 1 >= value.length()
            || !Character.isLowSurrogate(value.charAt(index + 1))) {
          throw new IllegalArgumentException(name + " contains an unpaired UTF-16 surrogate");
        }
        index++;
      } else if (Character.isLowSurrogate(current)) {
        throw new IllegalArgumentException(name + " contains an unpaired UTF-16 surrogate");
      }
    }
  }
}
