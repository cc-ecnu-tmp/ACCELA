package accela.pass.ir.transform.loop.interchange;

/** Prefers permutations that reduce the aggregate byte stride of the innermost loop. */
final class LoopInterchangeProfitability {
  private LoopInterchangeProfitability() {}

  static boolean isProfitable(LoopInterchangeCandidate candidate) {
    long currentCost = candidate.dependences().localityCost(1);
    long interchangedCost = candidate.dependences().localityCost(0);
    return interchangedCost < currentCost;
  }
}
