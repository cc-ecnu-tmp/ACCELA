package accela.pass.ir.transform.loop.tiling;

import accela.ir.Function;
import accela.pass.PassDescriptor;
import accela.pass.PreservedAnalyses;
import accela.pass.instrument.DecisionReasonCode;
import accela.pass.instrument.PassDecisionEmitter;
import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.dependence.DependenceAnalysis;
import accela.pass.ir.transform.loop.unroll.LoopUnrollAndJam;
import accela.pass.ir.instrument.PassInstrumentation;
import java.util.List;

/** Applies the existing proof-driven blocked schedule when the target model predicts a benefit. */
public final class CostModelLoopTiling {
  private final TargetCostModel costModel = new TargetCostModel();

  private CostModelLoopTiling() {}

  public static final class Pass implements FunctionPass {
    private final PassInstrumentation instrumentation;
    private final PassDescriptor descriptor;
    private final int occurrence;
    private final TargetCostModel costModel = new TargetCostModel();

    public Pass(PassInstrumentation instrumentation, PassDescriptor descriptor, int occurrence) {
      this.instrumentation = instrumentation;
      this.descriptor = descriptor;
      this.occurrence = occurrence;
    }

    @Override
    public PreservedAnalyses run(Function function, FunctionAnalysisManager fam) {
      PassDecisionEmitter decision = instrumentation.decisionEmitter(
          descriptor, occurrence, "function", "@" + function.getName());
      decision.candidate(DecisionReasonCode.CANDIDATE_MATCHED);
      List<LoopAnalysis.Loop> loops = fam.getResult(LoopAnalysis.class, function).loops();
      List<InductionVariableAnalysis.Induction> inductions =
          fam.getResult(InductionVariableAnalysis.class, function).allInductions();
      boolean profitablePlan = loops.stream()
          .map(loop -> costModel.choose(function, loop, loops))
          .filter(LoopSchedulePlan::profitable)
          .anyMatch(plan -> hasDependenceProof(loops, inductions, plan));
      boolean changed = profitablePlan && LoopUnrollAndJam.run(function, fam);
      if (changed) {
        decision.applied(DecisionReasonCode.APPLIED_PROFITABLE);
        return PreservedAnalyses.none();
      }
      decision.rejected(DecisionReasonCode.REJECTED_PROFITABILITY);
      return PreservedAnalyses.all();
    }

    private boolean hasDependenceProof(
        List<LoopAnalysis.Loop> loops,
        List<InductionVariableAnalysis.Induction> inductions,
        LoopSchedulePlan plan) {
      for (LoopAnalysis.Loop outer : loops) {
        for (LoopAnalysis.Loop inner : loops) {
          if (outer == inner || !outer.contains(inner.header())
              || outer.blocks().size() <= inner.blocks().size()) continue;
          var outerIv = inductions.stream().filter(iv -> iv.loop() == outer).findFirst().orElse(null);
          var innerIv = inductions.stream().filter(iv -> iv.loop() == inner).findFirst().orElse(null);
          if (outerIv == null || innerIv == null) continue;
          DependenceAnalysis.Result dependence = DependenceAnalysis.analyze(
              List.of(outerIv.phi(), innerIv.phi()), outer.blocks().stream().toList());
          if (dependence.isSafeToJam(
              outerIv.phi(), innerIv.phi(), outerIv.step(), plan.tile())) return true;
        }
      }
      return false;
    }
  }
}
