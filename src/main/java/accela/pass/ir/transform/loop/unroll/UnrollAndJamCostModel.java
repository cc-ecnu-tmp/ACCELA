package accela.pass.ir.transform.loop.unroll;

import accela.ir.Instruction;
import accela.ir.Type;
import accela.pass.ir.analysis.dependence.DependenceAnalysis;
import java.util.List;

/** Chooses a small jam factor without exhausting the RISC-V integer or FP register files. */
final class UnrollAndJamCostModel {
  private static final int MAX_FACTOR = 8;
  // Keep scratch space for address calculation, the shared IV, and lowering temporaries.
  private static final int INTEGER_BUDGET = 18;
  private static final int FLOAT_BUDGET = 20;
  private static final int MAX_CLONED_INSTRUCTIONS = 96;

  private UnrollAndJamCostModel() {}

  static DependenceAnalysis.Result analyzeDependences(LoopUnrollAndJamCandidate candidate) {
    return DependenceAnalysis.analyze(
        List.of(
            candidate.outerInduction().phi(),
            candidate.innerInduction().phi()),
        List.of(candidate.innerPreheader(), candidate.innerBody(), candidate.innerExit()));
  }

  static int chooseFactor(
      LoopUnrollAndJamCandidate candidate, DependenceAnalysis.Result dependences) {
    Pressure pressure = estimatePressure(candidate);
    int bodySize = (int) candidate.innerBody().getInstructions().stream()
        .filter(instruction -> instruction.getOpcode() != Instruction.Opcode.PHI
            && !instruction.isTerminator())
        .count();
    for (int factor = highestPowerOfTwoAtMost(MAX_FACTOR); factor >= 2; factor /= 2) {
      if (bodySize * factor <= MAX_CLONED_INSTRUCTIONS
          && pressure.integerShared + factor * pressure.integerPerLane <= INTEGER_BUDGET
          && pressure.floatShared + factor * pressure.floatPerLane <= FLOAT_BUDGET
          && dependences.isSafeToJam(
              candidate.outerInduction().phi(),
              candidate.innerInduction().phi(),
              candidate.outerInduction().step(),
              factor)) {
        return factor;
      }
    }
    return 1;
  }

  private static int highestPowerOfTwoAtMost(int limit) {
    int factor = 1;
    while (factor <= limit / 2) factor *= 2;
    return factor;
  }

  private static Pressure estimatePressure(LoopUnrollAndJamCandidate candidate) {
    int integerPerLane = 1; // outer induction value
    int floatPerLane = 0;
    int integerShared = 1; // inner induction value
    int floatShared = 0;

    for (Instruction phi : candidate.innerBody().getInstructions()) {
      if (phi.getOpcode() != Instruction.Opcode.PHI) break;
      if (phi == candidate.innerInduction().phi()) continue;
      if (isFloatValue(phi.getType())) floatPerLane += registerUnits(phi.getType());
      else integerPerLane += registerUnits(phi.getType());
    }

    for (Instruction instruction : candidate.innerPreheader().getInstructions()) {
      if (instruction.isTerminator()
          || !LoopUnrollAndJamCandidate.dependsOn(
              instruction, candidate.outerInduction().phi())
          || instruction.getUses().stream()
              .noneMatch(use -> use.getUser().getParent() == candidate.innerBody())) continue;
      if (isFloatValue(instruction.getType()))
        floatPerLane += registerUnits(instruction.getType());
      else if (instruction.hasResult()) integerPerLane += registerUnits(instruction.getType());
    }

    for (Instruction instruction : candidate.innerBody().getInstructions()) {
      if (instruction.getOpcode() == Instruction.Opcode.PHI || instruction.isTerminator()) continue;
      if (LoopUnrollAndJamCandidate.dependsOn(
          instruction, candidate.outerInduction().phi())) continue;
      if (isFloatValue(instruction.getType())) floatShared += registerUnits(instruction.getType());
      else if (instruction.hasResult()) integerShared += registerUnits(instruction.getType());
    }
    return new Pressure(integerPerLane, floatPerLane, integerShared, floatShared);
  }

  private static boolean isFloatValue(Type type) {
    return type == Type.FLOAT || type.isVector() && type.getElementType() == Type.FLOAT;
  }

  private static int registerUnits(Type type) {
    return type.isVector() ? type.getLaneCount() : 1;
  }

  private record Pressure(
      int integerPerLane, int floatPerLane, int integerShared, int floatShared) {}
}
