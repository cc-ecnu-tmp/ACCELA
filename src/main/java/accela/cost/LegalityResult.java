package accela.cost;

import java.util.Objects;

/** Fail-closed candidate legality result. */
public record LegalityResult(Status status, String obligation, String detail) {
  public enum Status { PROVED, REJECTED, UNKNOWN }

  public LegalityResult {
    Objects.requireNonNull(status, "status");
    if (obligation == null || obligation.isBlank()) throw new IllegalArgumentException("obligation is required");
    if (detail == null || detail.isBlank()) throw new IllegalArgumentException("detail is required");
  }

  public boolean mayApply() { return status == Status.PROVED; }
}
