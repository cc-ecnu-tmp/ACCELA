package accela.pass.instrument;

import accela.pass.PassDescriptor;
import java.util.Objects;

/** One real candidate/matcher decision emitted by an instrumented transform. */
public record DecisionRemark(
    String passId,
    int occurrence,
    PassDescriptor.Stage stage,
    String targetKind,
    String targetName,
    DecisionStatus decision,
    DecisionReasonCode reason,
    String legalityObligationId) implements OptimizationRemark {

  public DecisionRemark {
    Objects.requireNonNull(passId, "passId");
    Objects.requireNonNull(stage, "stage");
    Objects.requireNonNull(targetKind, "targetKind");
    Objects.requireNonNull(targetName, "targetName");
    Objects.requireNonNull(decision, "decision");
    Objects.requireNonNull(reason, "reason");
    if (occurrence < 1) throw new IllegalArgumentException("occurrence must be positive");
    if (reason.status() != decision) {
      throw new IllegalArgumentException(
          "reason " + reason + " is not valid for decision " + decision);
    }
    if (reason == DecisionReasonCode.REJECTED_LEGALITY) {
      if (legalityObligationId == null || legalityObligationId.isBlank()) {
        throw new IllegalArgumentException(
            "rejected_legality requires a non-empty legality obligation id");
      }
    } else if (legalityObligationId != null) {
      throw new IllegalArgumentException(
          "legality obligation id is only valid with rejected_legality");
    }
  }
}
