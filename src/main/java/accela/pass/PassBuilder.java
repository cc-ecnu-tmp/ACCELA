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
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.function.BiFunction;

/** Builds the project's registered, reproducible IR pass pipelines. */
public final class PassBuilder {
  /** Creates pass instrumentation with always-on verification and optional reporting. */
  public PassInstrumentation buildIRInstrumentation(boolean printReports) {
    return PassInstrumentation.enabled(printReports);
  }

  /** Creates a fresh module analysis manager. */
  public ModuleAnalysisManager buildModuleAnalysisManager() {
    return new ModuleAnalysisManager();
  }

  /** Creates a fresh function analysis manager. */
  public FunctionAnalysisManager buildFunctionAnalysisManager() {
    FunctionAnalysisManager fam = new FunctionAnalysisManager();
    fam.registerPass(DominatorTreeAnalysis.class, new DominatorTreeAnalysis());
    fam.registerPass(InductionVariableAnalysis.class, new InductionVariableAnalysis());
    fam.registerPass(LoopAnalysis.class, new LoopAnalysis());
    fam.registerPass(PostDominatorTreeAnalysis.class, new PostDominatorTreeAnalysis());
    fam.registerPass(ScalarEvolutionAnalysis.class, new ScalarEvolutionAnalysis());
    return fam;
  }

  /** Builds the production FULL pipeline. Environment variables do not alter this pipeline. */
  public ModulePassManager buildIRO0Pipeline(PassInstrumentation instrumentation) {
    return buildIRO0Pipeline(instrumentation, PipelineProfile.full());
  }

  /** Builds a deterministic ablation pipeline from stable FULL-pipeline occurrence numbers. */
  public ModulePassManager buildIRO0Pipeline(
      PassInstrumentation instrumentation, PipelineProfile profile) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    Objects.requireNonNull(profile, "profile");
    Schedule schedule = new Schedule(profile);

    FunctionPassManager initial = new FunctionPassManager(instrumentation);
    schedule.function(initial, PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());
    schedule.function(initial, PassRegistry.IR_SROA, new SROA.Pass());
    schedule.function(initial, PassRegistry.IR_MEM2REG, new Mem2Reg.Pass());
    schedule.function(initial, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(initial, PassRegistry.IR_SCCP, new SCCP.Pass());
    schedule.function(initial, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(initial, PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(initial, PassRegistry.IR_SROA, new SROA.Pass());
    schedule.function(initial, PassRegistry.IR_SCCP, new SCCP.Pass());
    schedule.function(initial, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(initial, PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(initial, PassRegistry.IR_INST_COMBINE, new InstCombine.Pass());
    schedule.function(initial, PassRegistry.IR_ADCE, new ADCE.Pass());
    schedule.function(initial, PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());

    FunctionPassManager globalMemory = new FunctionPassManager(instrumentation);
    schedule.function(globalMemory, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());

    FunctionPassManager preInline = new FunctionPassManager(instrumentation);
    schedule.function(preInline, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(preInline, PassRegistry.IR_TAIL_RECURSION_ELIMINATION,
        new TailRecursionElimination.Pass());

    FunctionPassManager postInline = new FunctionPassManager(instrumentation);
    schedule.function(postInline, PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());
    schedule.function(postInline, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(postInline, PassRegistry.IR_SCCP, new SCCP.Pass());
    schedule.function(postInline, PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(postInline, PassRegistry.IR_INST_COMBINE, new InstCombine.Pass());
    schedule.function(postInline, PassRegistry.IR_ADCE, new ADCE.Pass());
    schedule.function(postInline, PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());

    FunctionPassManager postIpsccp = new FunctionPassManager(instrumentation);
    schedule.function(postIpsccp, PassRegistry.IR_INDVAR_DOMAIN_SIMPLIFY,
        new IndVarSimplify.DomainPass());
    schedule.functionObserved(postIpsccp, PassRegistry.IR_AFFINE_LOOP_SUMMARIZATION,
        (descriptor, occurrence) ->
            instrumentation.isEnabled()
                ? new AffineLoopSummarization.Pass(instrumentation, descriptor, occurrence)
                : new AffineLoopSummarization.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_REDUCTION_PUSHDOWN,
        new ReductionPushdown.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_INTERCHANGE,
        new LoopInterchange.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_ROTATE, new LoopRotate.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LICM, new LICM.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_EARLY_CSE, new EarlyCSE.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_UNROLL_AND_JAM,
        new LoopUnrollAndJam.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_UNROLL, new LoopUnroll.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_UNROLL, new LoopUnroll.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_INDVAR_SIMPLIFY,
        new IndVarSimplify.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_INST_SIMPLIFY, new InstSimplify.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_SIMPLIFY_CFG, new SimplifyCFG.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_GVN, new GVN.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_STRENGTH_REDUCE,
        new LoopStrengthReduce.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_LOAD_ROTATION,
        new LoopRotate.LoadEliminationPass());
    schedule.function(postIpsccp, PassRegistry.IR_LOOP_LOAD_ELIMINATION,
        new LoopLoadElimination.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_POINTER_LFTR,
        new IndVarSimplify.LFTRPass());
    schedule.function(postIpsccp, PassRegistry.IR_LICM, new LICM.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_STRENGTH_REDUCTION,
        new StrengthReduction.Pass());
    schedule.function(postIpsccp, PassRegistry.IR_ADCE, new ADCE.Pass());

    ModulePassManager module = new ModulePassManager(instrumentation);
    module.addPass(new ModuleToFunctionPassAdaptor(initial));
    module.addPass(new ModuleToFunctionPassAdaptor(globalMemory));
    schedule.module(module, PassRegistry.IR_DEAD_STORE_ELIMINATION,
        new DeadStoreElimination.Pass());
    schedule.module(module, PassRegistry.IR_GLOBAL_DCE, new ADCE.GlobalPass());
    schedule.module(module, PassRegistry.IR_GLOBAL_OPT, new GlobalOpt.Pass());
    schedule.module(module, PassRegistry.IR_GLOBAL_SROA, new SROA.GlobalPass());
    schedule.module(module, PassRegistry.IR_IPSCCP, new IPSCCP.Pass());
    schedule.moduleObserved(module, PassRegistry.IR_RANKED_RECURRENCE_TABULATION,
        (descriptor, occurrence) ->
            instrumentation.isEnabled()
                ? new RankedRecurrenceTabulation.Pass(instrumentation, descriptor, occurrence)
                : new RankedRecurrenceTabulation.Pass());
    module.addPass(new ModuleToFunctionPassAdaptor(preInline));
    schedule.module(module, PassRegistry.IR_INLINER, new Inliner.Pass());
    module.addPass(new ModuleToFunctionPassAdaptor(postInline));
    schedule.module(module, PassRegistry.IR_IPSCCP, new IPSCCP.Pass());
    schedule.module(module, PassRegistry.IR_GLOBAL_DCE, new ADCE.GlobalPass());
    schedule.module(module, PassRegistry.IR_GLOBAL_OPT, new GlobalOpt.Pass());
    module.addPass(new ModuleToFunctionPassAdaptor(postIpsccp));
    schedule.module(module, PassRegistry.IR_GLOBAL_DCE, new ADCE.GlobalPass());

    schedule.verifyComplete();
    return module;
  }

  public ModulePassManager buildIRO0Pipeline() {
    return buildIRO0Pipeline(PassInstrumentation.noop(), PipelineProfile.full());
  }

  private static final class Schedule {
    private final PipelineProfile profile;
    private final Map<String, Integer> occurrences = new LinkedHashMap<>();

    Schedule(PipelineProfile profile) {
      this.profile = profile;
    }

    void function(FunctionPassManager manager, String passId, FunctionPass pass) {
      PassDescriptor descriptor = descriptor(passId, PassDescriptor.Stage.IR_FUNCTION);
      int occurrence = reserve(descriptor);
      if (profile.isEnabled(passId, occurrence)) manager.addPass(pass, descriptor, occurrence);
    }

    void module(ModulePassManager manager, String passId, ModulePass pass) {
      PassDescriptor descriptor = descriptor(passId, PassDescriptor.Stage.IR_MODULE);
      int occurrence = reserve(descriptor);
      if (profile.isEnabled(passId, occurrence)) manager.addPass(pass, descriptor, occurrence);
    }

    void functionObserved(
        FunctionPassManager manager,
        String passId,
        BiFunction<PassDescriptor, Integer, FunctionPass> factory) {
      PassDescriptor descriptor = descriptor(passId, PassDescriptor.Stage.IR_FUNCTION);
      int occurrence = reserve(descriptor);
      if (profile.isEnabled(passId, occurrence)) {
        manager.addPass(factory.apply(descriptor, occurrence), descriptor, occurrence);
      }
    }

    void moduleObserved(
        ModulePassManager manager,
        String passId,
        BiFunction<PassDescriptor, Integer, ModulePass> factory) {
      PassDescriptor descriptor = descriptor(passId, PassDescriptor.Stage.IR_MODULE);
      int occurrence = reserve(descriptor);
      if (profile.isEnabled(passId, occurrence)) {
        manager.addPass(factory.apply(descriptor, occurrence), descriptor, occurrence);
      }
    }

    void verifyComplete() {
      for (PassDescriptor descriptor : profile.registry().all()) {
        if (!descriptor.stage().isIr()) continue;
        int actual = occurrences.getOrDefault(descriptor.id(), 0);
        if (actual != descriptor.fullPipelineOccurrences()) {
          throw new IllegalStateException("registered FULL occurrence count for '" + descriptor.id()
              + "' is " + descriptor.fullPipelineOccurrences() + ", but pipeline schedules " + actual);
        }
      }
    }

    private PassDescriptor descriptor(String id, PassDescriptor.Stage stage) {
      PassDescriptor descriptor = profile.registry().require(id);
      if (descriptor.stage() != stage) {
        throw new IllegalStateException("pass '" + id + "' registered for " + descriptor.stage()
            + " but scheduled for " + stage);
      }
      return descriptor;
    }

    private int reserve(PassDescriptor descriptor) {
      int occurrence = occurrences.merge(descriptor.id(), 1, Integer::sum);
      if (occurrence > descriptor.fullPipelineOccurrences()) {
        throw new IllegalStateException("pipeline schedules too many occurrences of '"
            + descriptor.id() + "'");
      }
      return occurrence;
    }
  }
}
