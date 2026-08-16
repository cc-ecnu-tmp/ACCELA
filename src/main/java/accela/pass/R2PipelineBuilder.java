package accela.pass;

import accela.pass.ir.FunctionAnalysisManager;
import accela.pass.ir.FunctionPass;
import accela.pass.ir.FunctionPassManager;
import accela.pass.ir.ModuleAnalysisManager;
import accela.pass.ir.ModulePass;
import accela.pass.ir.ModulePassManager;
import accela.pass.ir.ModuleToFunctionPassAdaptor;
import accela.pass.ir.analysis.DominatorTreeAnalysis;
import accela.pass.ir.analysis.InductionVariableAnalysis;
import accela.pass.ir.analysis.LoopAnalysis;
import accela.pass.ir.analysis.PostDominatorTreeAnalysis;
import accela.pass.ir.analysis.ScalarEvolutionAnalysis;
import accela.pass.ir.instrument.PassInstrumentation;
import accela.pass.ir.transform.ADCE;
import accela.pass.ir.transform.AffineLoopSummarization;
import accela.pass.ir.transform.DeadStoreElimination;
import accela.pass.ir.transform.EarlyCSE;
import accela.pass.ir.transform.GlobalOpt;
import accela.pass.ir.transform.IPSCCP;
import accela.pass.ir.transform.InstCombine;
import accela.pass.ir.transform.InstSimplify;
import accela.pass.ir.transform.LICM;
import accela.pass.ir.transform.Mem2Reg;
import accela.pass.ir.transform.ReductionPushdown;
import accela.pass.ir.transform.SCCP;
import accela.pass.ir.transform.SROA;
import accela.pass.ir.transform.StrengthReduction;
import accela.pass.ir.transform.TailRecursionElimination;
import accela.pass.ir.transform.gvn.GVN;
import accela.pass.ir.transform.indvars.IndVarSimplify;
import accela.pass.ir.transform.inliner.Inliner;
import accela.pass.ir.transform.loop.interchange.LoopInterchange;
import accela.pass.ir.transform.loop.load.LoopLoadElimination;
import accela.pass.ir.transform.loop.rotate.LoopRotate;
import accela.pass.ir.transform.loop.strength.LoopStrengthReduce;
import accela.pass.ir.transform.loop.unroll.LoopUnroll;
import accela.pass.ir.transform.loop.unroll.LoopUnrollAndJam;
import accela.pass.ir.transform.recurrence.RankedRecurrenceTabulation;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;

/** Constructs an IR pipeline directly from a complete, validated R2 occurrence profile. */
public final class R2PipelineBuilder {
  private final R2PipelineProfile profile;

  public R2PipelineBuilder(R2PipelineProfile profile) {
    this.profile = java.util.Objects.requireNonNull(profile, "profile");
  }

  public FunctionAnalysisManager buildFunctionAnalysisManager() {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(PostDominatorTreeAnalysis.class, new PostDominatorTreeAnalysis());
    fam.registerPass(ScalarEvolutionAnalysis.class, new ScalarEvolutionAnalysis());
    return fam;
  }

  public ModuleAnalysisManager buildModuleAnalysisManager() {
    return new ModuleAnalysisManager();
  }

  public ModulePassManager buildIRPipeline(PassInstrumentation instrumentation) {
    if (instrumentation == null) throw new IllegalArgumentException("instrumentation is required");
    Cursor schedule = new Cursor(profile);
    FunctionPassManager initial = new FunctionPassManager(instrumentation);
    schedule.function(initial, R2PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());
    schedule.function(initial, R2PassRegistry.IR_SROA, new SROA.Pass());
    schedule.function(initial, R2PassRegistry.IR_MEM2REG, new Mem2Reg.Pass());
    schedule.function(initial, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(initial, R2PassRegistry.IR_SCCP, new SCCP.Pass());
    schedule.function(initial, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(initial, R2PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(initial, R2PassRegistry.IR_SROA, new SROA.Pass());
    schedule.function(initial, R2PassRegistry.IR_SCCP, new SCCP.Pass());
    schedule.function(initial, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(initial, R2PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(initial, R2PassRegistry.IR_INST_COMBINE, new InstCombine.Pass());
    schedule.function(initial, R2PassRegistry.IR_ADCE, new ADCE.Pass());
    schedule.function(initial, R2PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());

    FunctionPassManager globalMemory = new FunctionPassManager(instrumentation);
    schedule.function(globalMemory, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());

    FunctionPassManager preInline = new FunctionPassManager(instrumentation);
    schedule.function(preInline, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(preInline, R2PassRegistry.IR_TAIL_RECURSION,
        new TailRecursionElimination.Pass());

    FunctionPassManager postInline = new FunctionPassManager(instrumentation);
    schedule.function(postInline, R2PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());
    schedule.function(postInline, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(postInline, R2PassRegistry.IR_SCCP, new SCCP.Pass());
    schedule.function(postInline, R2PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(postInline, R2PassRegistry.IR_INST_COMBINE, new InstCombine.Pass());
    schedule.function(postInline, R2PassRegistry.IR_ADCE, new ADCE.Pass());
    schedule.function(postInline, R2PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());

    FunctionPassManager late = new FunctionPassManager(instrumentation);
    schedule.function(late, R2PassRegistry.IR_INDVAR_DOMAIN,
        new IndVarSimplify.DomainPass());
    schedule.function(late, R2PassRegistry.IR_AFFINE_SUMMARY,
        new AffineLoopSummarization.Pass());
    schedule.function(late, R2PassRegistry.IR_REDUCTION_PUSHDOWN,
        new ReductionPushdown.Pass());
    schedule.function(late, R2PassRegistry.IR_LOOP_INTERCHANGE,
        new LoopInterchange.Pass());
    schedule.function(late, R2PassRegistry.IR_LOOP_ROTATE, new LoopRotate.Pass());
    schedule.function(late, R2PassRegistry.IR_LICM, new LICM.Pass());
    schedule.function(late, R2PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(late, R2PassRegistry.IR_UNROLL_AND_JAM,
        new LoopUnrollAndJam.Pass());
    schedule.function(late, R2PassRegistry.IR_UNROLL, new LoopUnroll.Pass());
    schedule.function(late, R2PassRegistry.IR_UNROLL, new LoopUnroll.Pass());
    schedule.function(late, R2PassRegistry.IR_INDVAR_SIMPLIFY,
        new IndVarSimplify.Pass());
    schedule.function(late, R2PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(late, R2PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());
    schedule.function(late, R2PassRegistry.IR_GVN, new GVN.Pass());
    schedule.function(late, R2PassRegistry.IR_LOOP_STRENGTH,
        new LoopStrengthReduce.Pass());
    schedule.function(late, R2PassRegistry.IR_LOOP_LOAD_ROTATE,
        new LoopRotate.LoadEliminationPass());
    schedule.function(late, R2PassRegistry.IR_LOOP_LOAD_ELIMINATION,
        new LoopLoadElimination.Pass());
    schedule.function(late, R2PassRegistry.IR_POINTER_LFTR,
        new IndVarSimplify.LFTRPass());
    schedule.function(late, R2PassRegistry.IR_LICM, new LICM.Pass());
    schedule.function(late, R2PassRegistry.IR_STRENGTH, new StrengthReduction.Pass());
    schedule.function(late, R2PassRegistry.IR_ADCE, new ADCE.Pass());

    ModulePassManager module = new ModulePassManager(instrumentation);
    module.addPass(new ModuleToFunctionPassAdaptor(initial));
    module.addPass(new ModuleToFunctionPassAdaptor(globalMemory));
    schedule.module(module, R2PassRegistry.IR_DEAD_STORE_ELIMINATION,
        new DeadStoreElimination.Pass());
    schedule.module(module, R2PassRegistry.IR_GLOBAL_DCE, new ADCE.GlobalPass());
    schedule.module(module, R2PassRegistry.IR_GLOBAL_OPT, new GlobalOpt.Pass());
    schedule.module(module, R2PassRegistry.IR_GLOBAL_SROA, new SROA.GlobalPass());
    schedule.module(module, R2PassRegistry.IR_IPSCCP, new IPSCCP.Pass());
    schedule.module(module, R2PassRegistry.IR_RRT, new RankedRecurrenceTabulation.Pass());
    module.addPass(new ModuleToFunctionPassAdaptor(preInline));
    schedule.module(module, R2PassRegistry.IR_INLINER, new Inliner.Pass());
    module.addPass(new ModuleToFunctionPassAdaptor(postInline));
    schedule.module(module, R2PassRegistry.IR_IPSCCP, new IPSCCP.Pass());
    schedule.module(module, R2PassRegistry.IR_GLOBAL_DCE, new ADCE.GlobalPass());
    schedule.module(module, R2PassRegistry.IR_GLOBAL_OPT, new GlobalOpt.Pass());
    module.addPass(new ModuleToFunctionPassAdaptor(late));
    schedule.module(module, R2PassRegistry.IR_GLOBAL_DCE, new ADCE.GlobalPass());
    schedule.verifyIrComplete();
    return module;
  }

  private static final class Cursor {
    private final R2PipelineProfile profile;
    private final java.util.Map<String, R2ScheduleState.Decision> decisions;
    private final java.util.Map<String, Integer> familyOccurrences = new java.util.LinkedHashMap<>();
    private final java.util.Set<String> consumed = new java.util.LinkedHashSet<>();

    Cursor(R2PipelineProfile profile) {
      this.profile = profile;
      java.util.LinkedHashMap<String, R2ScheduleState.Decision> byId =
          new java.util.LinkedHashMap<>();
      for (R2ScheduleState.Decision decision : profile.decisions()) {
        byId.put(decision.occurrenceId(), decision);
      }
      decisions = java.util.Map.copyOf(byId);
    }

    void function(FunctionPassManager manager, String family, FunctionPass pass) {
      R2ScheduleState.Decision decision = next(family, R2PassOccurrence.Scope.FUNCTION);
      if (decision.action() == R2ScheduleState.Action.APPLY) manager.addPass(pass);
    }

    void module(ModulePassManager manager, String family, ModulePass pass) {
      R2ScheduleState.Decision decision = next(family, R2PassOccurrence.Scope.MODULE);
      if (decision.action() == R2ScheduleState.Action.APPLY) manager.addPass(pass);
    }

    private R2ScheduleState.Decision next(String family, R2PassOccurrence.Scope scope) {
      int occurrenceNumber = familyOccurrences.merge(family, 1, Integer::sum);
      String occurrenceId = family + "." + occurrenceNumber;
      R2ScheduleState.Decision decision = decisions.get(occurrenceId);
      if (decision == null) {
        throw new IllegalStateException("R2 profile omits occurrence " + occurrenceId);
      }
      R2PassOccurrence occurrence = profile.registry().require(decision.occurrenceId());
      if (occurrence.stage() != PassDescriptor.Stage.IR
          || occurrence.scope() != scope || !occurrence.familyId().equals(family)) {
        throw new IllegalStateException("R2 profile/pipeline drift at " + occurrence.id()
            + "; expected " + family + " " + scope);
      }
      if (!consumed.add(occurrence.id())) {
        throw new IllegalStateException("R2 occurrence constructed twice: " + occurrence.id());
      }
      return decision;
    }

    void verifyIrComplete() {
      java.util.List<String> expected = profile.registry().all().stream()
          .filter(occurrence -> occurrence.stage() == PassDescriptor.Stage.IR)
          .map(R2PassOccurrence::id).toList();
      if (!consumed.equals(new java.util.LinkedHashSet<>(expected))) {
        java.util.LinkedHashSet<String> missing = new java.util.LinkedHashSet<>(expected);
        missing.removeAll(consumed);
        throw new IllegalStateException("R2 IR pipeline occurrence drift; missing " + missing);
      }
    }
  }
}
