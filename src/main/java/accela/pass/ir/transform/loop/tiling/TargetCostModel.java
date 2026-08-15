package accela.pass.ir.transform.loop.tiling;

import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.ir.Instruction;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.List;

/** Small static RV64GC schedule model; it never inspects benchmark identity or input data. */
public final class TargetCostModel {
  private static final List<Integer> TILES = List.of(4, 8, 16, 32);

  public LoopSchedulePlan choose(Function function, LoopAnalysis.Loop loop) {
    return choose(function, loop, List.of(loop));
  }

  /** Chooses a tile only for a perfect two-level affine-looking loop nest. */
  public LoopSchedulePlan choose(
      Function function, LoopAnalysis.Loop loop, List<LoopAnalysis.Loop> allLoops) {
    if (function == null || loop == null || loop.preheader() == null
        || directChildren(loop, allLoops).size() != 1) {
      return new LoopSchedulePlan(4, Long.MAX_VALUE, false);
    }
    long memoryOps = loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.LOAD
            || instruction.getOpcode() == Instruction.Opcode.STORE)
        .count();
    long branches = loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode().isTerminator())
        .count();
    long addressOps = loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.GEP)
        .count();
    long registerPressure = loop.blocks().stream()
        .flatMap(block -> block.getInstructions().stream())
        .filter(instruction -> instruction.getOpcode() == Instruction.Opcode.PHI)
        .count() + addressOps;
    if (memoryOps == 0 || addressOps == 0 || registerPressure > 32) {
      return new LoopSchedulePlan(4, Long.MAX_VALUE, false);
    }
    int tile = registerPressure <= 8 && memoryOps >= 16 ? 32
        : registerPressure <= 16 && memoryOps >= 8 ? 16
        : memoryOps >= 4 ? 8 : 4;
    long traffic = Math.multiplyExact(memoryOps, 4L);
    long scalarCost = Math.addExact(traffic,
        Math.addExact(Math.multiplyExact(branches, 6L), Math.multiplyExact(addressOps, 2L)));
    long tiledCost = Math.addExact(
        Math.max(1L, traffic / tile),
        Math.addExact(Math.multiplyExact(branches, 3L), Math.multiplyExact(registerPressure, 2L)));
    boolean profitable = scalarCost > tiledCost;
    return new LoopSchedulePlan(tile, tiledCost, profitable);
  }

  private static List<LoopAnalysis.Loop> directChildren(
      LoopAnalysis.Loop outer, List<LoopAnalysis.Loop> allLoops) {
    List<LoopAnalysis.Loop> nested = allLoops.stream()
        .filter(loop -> loop != outer && outer.contains(loop.header())).toList();
    List<LoopAnalysis.Loop> direct = new ArrayList<>();
    for (LoopAnalysis.Loop candidate : nested) {
      boolean hasParentBetween = nested.stream().anyMatch(parent ->
          parent != candidate && parent.contains(candidate.header())
              && outer.contains(parent.header()));
      if (!hasParentBetween) direct.add(candidate);
    }
    return direct;
  }
}
