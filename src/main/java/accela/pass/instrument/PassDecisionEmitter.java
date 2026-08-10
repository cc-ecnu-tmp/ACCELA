package accela.pass.instrument;

import accela.pass.PassDescriptor;
import java.util.Locale;
import java.util.Objects;

/** Fail-fast stateful emitter for one concrete optimization candidate. */
public final class PassDecisionEmitter {
  private final PassRemarkSink sink;
  private final PassDescriptor descriptor;
  private final int occurrence;
  private final String targetKind;
  private final String targetName;
  private boolean candidateEmitted;
  private boolean terminalEmitted;

  PassDecisionEmitter(
      PassRemarkSink sink,
      PassDescriptor descriptor,
      int occurrence,
      String targetKind,
      String targetName) {
    this.sink = Objects.requireNonNull(sink, "sink");
    this.descriptor = Objects.requireNonNull(descriptor, "descriptor");
    this.targetKind = requireText(targetKind, "targetKind");
    this.targetName = requireText(targetName, "targetName");
    if (occurrence < 1 || occurrence > descriptor.fullPipelineOccurrences()) {
      throw new IllegalArgumentException(
          "invalid occurrence " + occurrence + " for " + descriptor.id());
    }
    this.occurrence = occurrence;
  }

  public void candidate(DecisionReasonCode reason) {
    requireReason(reason, DecisionStatus.CANDIDATE);
    if (candidateEmitted) throw new IllegalStateException("candidate was already emitted");
    if (terminalEmitted) throw new IllegalStateException("candidate is already terminal");
    candidateEmitted = true;
    emit(DecisionStatus.CANDIDATE, reason, null);
  }

  public void applied(DecisionReasonCode reason) {
    terminal(DecisionStatus.APPLIED, reason, null);
  }

  public void rejected(DecisionReasonCode reason) {
    if (reason == DecisionReasonCode.REJECTED_LEGALITY) {
      throw new IllegalArgumentException(
          "rejected_legality requires rejectedLegality(obligationId)");
    }
    terminal(DecisionStatus.REJECTED, reason, null);
  }

  public void rejectedLegality(String obligationId) {
    descriptor.requireLegalityObligation(obligationId);
    terminal(DecisionStatus.REJECTED, DecisionReasonCode.REJECTED_LEGALITY, obligationId);
  }

  private void terminal(
      DecisionStatus status,
      DecisionReasonCode reason,
      String legalityObligationId) {
    requireReason(reason, status);
    if (!candidateEmitted) {
      throw new IllegalStateException(
          status.name().toLowerCase(Locale.ROOT) + " requires a candidate event");
    }
    if (terminalEmitted) throw new IllegalStateException("candidate already has a terminal decision");
    terminalEmitted = true;
    emit(status, reason, legalityObligationId);
  }

  private void emit(
      DecisionStatus status,
      DecisionReasonCode reason,
      String legalityObligationId) {
    sink.accept(new DecisionRemark(descriptor.id(), occurrence, descriptor.stage(), targetKind,
        targetName, status, reason, legalityObligationId));
  }

  private static void requireReason(DecisionReasonCode reason, DecisionStatus status) {
    Objects.requireNonNull(reason, "reason");
    if (reason.status() != status) {
      throw new IllegalArgumentException(
          "reason " + reason + " is not valid for decision " + status);
    }
  }

  private static String requireText(String value, String name) {
    Objects.requireNonNull(value, name);
    if (value.isBlank()) throw new IllegalArgumentException(name + " must not be blank");
    return value;
  }
}
