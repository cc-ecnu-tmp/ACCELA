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
    DecisionReasonCode reason) implements OptimizationRemark {

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
  }
}
