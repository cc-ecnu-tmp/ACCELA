package accela.pass.instrument;

/** Controlled reason vocabulary for pass decision events. */
public enum DecisionReasonCode {
  CANDIDATE_MATCHED(DecisionStatus.CANDIDATE),
  APPLIED_PROFITABLE(DecisionStatus.APPLIED),
  APPLIED_CANONICALIZATION(DecisionStatus.APPLIED),
  REJECTED_LEGALITY(DecisionStatus.REJECTED),
  REJECTED_PROFITABILITY(DecisionStatus.REJECTED),
  REJECTED_UNSUPPORTED_SHAPE(DecisionStatus.REJECTED),
  REJECTED_RESOURCE_LIMIT(DecisionStatus.REJECTED),
  REJECTED_NO_BENEFIT(DecisionStatus.REJECTED);

  private final DecisionStatus status;

  DecisionReasonCode(DecisionStatus status) {
    this.status = status;
  }

  public DecisionStatus status() {
    return status;
  }
}
