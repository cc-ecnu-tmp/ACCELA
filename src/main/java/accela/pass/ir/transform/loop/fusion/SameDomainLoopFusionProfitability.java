package accela.pass.ir.transform.loop.fusion;

import accela.ir.Instruction;
import accela.pass.SameDomainLoopFusionCandidate;

/** Conservative code-size and live-state model for same-domain fusion. */
final class SameDomainLoopFusionProfitability {
  static final int MAX_FUSED_PAYLOAD_INSTRUCTIONS = 32;
  static final int MAX_LIVE_RECURRENCES = 8;

  record Plan(int payloadInstructions, int liveRecurrences, int eliminatedInstructions) {}

  record Result(Plan plan, String rejectedObligationId) {
    Result {
      if ((plan == null) == (rejectedObligationId == null)) {
        throw new IllegalArgumentException(
            "exactly one of plan and rejectedObligationId is required");
      }
    }

    static Result profitable(Plan plan) {
      return new Result(plan, null);
    }

    static Result rejected() {
      return new Result(null, SameDomainLoopFusionCandidate.PROFITABILITY);
    }
  }

  private SameDomainLoopFusionProfitability() {}

  static Result evaluate(SameDomainLoopFusionMatcher.Candidate candidate) {
    int payload = payloadInstructions(candidate.first()) + payloadInstructions(candidate.second());
    int recurrences = candidate.first().recurrencePhis().size()
        + candidate.second().recurrencePhis().size();
    int forwardedLoads = candidate.forwardings().stream()
        .mapToInt(forwarding -> forwarding.loads().size())
        .sum();
    int eliminated = candidate.second().domainInstructions().size()
        + 3 // compare, branch, and second induction update
        + forwardedLoads
        + candidate.forwardings().size();
    if (payload > MAX_FUSED_PAYLOAD_INSTRUCTIONS
        || recurrences > MAX_LIVE_RECURRENCES
        || eliminated < 3) {
      return Result.rejected();
    }
    return Result.profitable(new Plan(payload, recurrences, eliminated));
  }

  private static int payloadInstructions(
      SameDomainLoopFusionMatcher.CanonicalLoop loop) {
    int result = 0;
    for (Instruction instruction : loop.body().getInstructions()) {
      if (instruction == loop.nextInduction() || instruction.isTerminator()) continue;
      result++;
    }
    return result;
  }
}
