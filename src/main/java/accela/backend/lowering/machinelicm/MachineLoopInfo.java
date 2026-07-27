package accela.backend.lowering;

import accela.backend.machine.BlockOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.ir.BasicBlock;
import accela.ir.Function;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.ExactTripCount;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

/** Maps natural loops in the source IR onto their lowered machine blocks. */
record MachineLoopInfo(
    MachineBasicBlock preheader,
    List<MachineBasicBlock> blocks,
    boolean phiSplitPreheader,
    int tripCount) {
  static List<MachineLoopInfo> analyze(MachineFunction machineFunction) {
    Function sourceFunction = machineFunction.getBlocks().stream()
        .map(MachineBasicBlock::getSourceFunction)
        .filter(function -> function != null)
        .findFirst()
        .orElse(null);
    if (sourceFunction == null) return List.of();

    Map<BasicBlock, MachineBasicBlock> machineBlocks = new IdentityHashMap<>();
    for (MachineBasicBlock block : machineFunction.getBlocks()) {
      if (block.getSourceBlock() != null) machineBlocks.put(block.getSourceBlock(), block);
    }

    FunctionAnalysisManager analyses = new FunctionAnalysisManager();
    analyses.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    analyses.registerPass(LoopAnalysis.class, new LoopAnalysis());
    analyses.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    Map<LoopAnalysis.Loop, Integer> tripCounts = new IdentityHashMap<>();
    for (var induction :
        analyses.getResult(InductionVariableAnalysis.class, sourceFunction).allInductions()) {
      ExactTripCount trip = ExactTripCount.find(induction);
      if (trip != null) tripCounts.put(induction.loop(), trip.count());
    }
    List<MachineLoopInfo> result = new ArrayList<>();
    for (LoopAnalysis.Loop loop :
        analyses.getResult(LoopAnalysis.class, sourceFunction).loops()) {
      MachineBasicBlock preheader = machineBlocks.get(loop.preheader());
      boolean phiSplitPreheader = false;
      if (preheader == null) {
        // Phi elimination may already have split the entering edge even when the
        // source IR lacks a dedicated preheader. Reuse only that edge block.
        List<BasicBlock> outsidePredecessors = loop.header().getPredecessors().stream()
            .filter(block -> !loop.contains(block))
            .toList();
        if (outsidePredecessors.size() != 1) continue;
        preheader = splitPreheader(
            machineBlocks.get(outsidePredecessors.getFirst()),
            machineBlocks.get(loop.header()));
        phiSplitPreheader = preheader != null;
      }
      if (preheader == null) continue;
      List<MachineBasicBlock> blocks = machineFunction.getBlocks().stream()
          .filter(block ->
              block.getSourceBlock() != null && loop.blocks().contains(block.getSourceBlock()))
          .toList();
      if (!blocks.isEmpty()) {
        result.add(
            new MachineLoopInfo(
                preheader, blocks, phiSplitPreheader, tripCounts.getOrDefault(loop, -1)));
      }
    }
    return result;
  }

  private static MachineBasicBlock splitPreheader(
      MachineBasicBlock predecessor, MachineBasicBlock header) {
    if (predecessor == null || header == null) return null;
    return successors(predecessor).stream()
        .filter(block -> block.getSourceBlock() == null)
        .filter(block -> successors(block).equals(List.of(header)))
        .findFirst()
        .orElse(null);
  }

  private static List<MachineBasicBlock> successors(MachineBasicBlock block) {
    if (block.getInstructions().isEmpty()) return List.of();
    return block.getInstructions().getLast().getOperands().stream()
        .filter(BlockOperand.class::isInstance)
        .map(BlockOperand.class::cast)
        .map(BlockOperand::getBlock)
        .distinct()
        .toList();
  }
}
