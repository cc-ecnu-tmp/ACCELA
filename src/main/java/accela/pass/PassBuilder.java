package accela.pass;

import accela.pass.candidate.ExtendedAffineSummarizationCandidate;
import accela.pass.candidate.ArrayObjectPromotionCandidate;
import accela.pass.candidate.CostModelLoopTilingCandidate;
import accela.pass.candidate.FunctionSpecializationCandidate;
import accela.pass.candidate.NestedAddressRecurrenceCandidate;
import accela.pass.candidate.SysYRegionMemoryForwardingCandidate;
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
import accela.pass.ir.transform.scan.PrefixScanReuse;
import accela.pass.ir.transform.simplifycfg.SimplifyCFG;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.BiFunction;

/** Builds the project's registered, reproducible IR pass pipelines. */
public final class PassBuilder {
  private final CandidatePassProvider candidatePassProvider;

  /** Constructs the production pipeline with no candidate implementations registered. */
  public PassBuilder() {
    this(CandidatePassProvider.empty());
  }

  /** Explicit candidate-provider seam used by the development compiler and focused tests. */
  PassBuilder(CandidatePassProvider candidatePassProvider) {
    this.candidatePassProvider = Objects.requireNonNull(
        candidatePassProvider, "candidatePassProvider");
  }

  /**
   * Creates the development-only pipeline builder containing every screened candidate factory.
   *
   * <p>The judge-facing compiler deliberately uses {@link #PassBuilder()} instead. Candidate
   * implementations require decision instrumentation and remain lazily uninstantiated unless a
   * profile explicitly enables their stable identifier.
   */
  public static PassBuilder withStandardCandidateImplementations(
      PassInstrumentation instrumentation) {
    Objects.requireNonNull(instrumentation, "instrumentation");
    if (!instrumentation.isEnabled()) {
      throw new IllegalArgumentException(
          "standard candidate implementations require enabled decision instrumentation");
    }
    CandidatePassProvider provider = new CandidatePassProvider(
        Map.of(
            ExtendedAffineSummarizationCandidate.ID,
                ExtendedAffineSummarizationCandidate.factory(instrumentation),
            FiniteStateAccelerationCandidate.ID,
                FiniteStateAccelerationCandidate.functionFactory(instrumentation),
            SameDomainLoopFusionCandidate.ID,
                SameDomainLoopFusionCandidate.functionFactory(instrumentation),
            IntegerLinearTransitionCandidate.ID,
                IntegerLinearTransitionCandidate.functionFactory(instrumentation),
            PrefixScanReuse.ID,
                PrefixScanReuse.factory(instrumentation),
            SysYRegionMemoryForwardingCandidate.ID,
                SysYRegionMemoryForwardingCandidate.factory(instrumentation),
            ArrayObjectPromotionCandidate.ID,
                ArrayObjectPromotionCandidate.factory(instrumentation),
            NestedAddressRecurrenceCandidate.ID,
                NestedAddressRecurrenceCandidate.factory(instrumentation),
            CostModelLoopTilingCandidate.ID,
                CostModelLoopTilingCandidate.factory(instrumentation)),
        Map.of(
            Rrt2OnDemandMemoizationCandidate.ID,
                Rrt2OnDemandMemoizationCandidate.factory(instrumentation),
            FunctionSpecializationCandidate.ID,
                FunctionSpecializationCandidate.factory(instrumentation)));
    return new PassBuilder(provider);
  }

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
    Schedule schedule = new Schedule(profile, candidatePassProvider);

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

  private static IllegalStateException missingFactory(PassDescriptor descriptor) {
    return new IllegalStateException(
        "enabled candidate '" + descriptor.id() + "' has no registered "
            + descriptor.stage() + " factory");
  }

  /** Typed, immutable factories for candidates inserted into the real IR pipeline. */
  static final class CandidatePassProvider {
    private final Map<String, BiFunction<PassDescriptor, Integer, FunctionPass>> functionFactories;
    private final Map<String, BiFunction<PassDescriptor, Integer, ModulePass>> moduleFactories;

    CandidatePassProvider(
        Map<String, BiFunction<PassDescriptor, Integer, FunctionPass>> functionFactories,
        Map<String, BiFunction<PassDescriptor, Integer, ModulePass>> moduleFactories) {
      this.functionFactories = copyFactories(functionFactories, "functionFactories");
      this.moduleFactories = copyFactories(moduleFactories, "moduleFactories");
    }

    static CandidatePassProvider empty() {
      return new CandidatePassProvider(Map.of(), Map.of());
    }

    BiFunction<PassDescriptor, Integer, FunctionPass> functionFactory(String id) {
      return functionFactories.get(id);
    }

    BiFunction<PassDescriptor, Integer, ModulePass> moduleFactory(String id) {
      return moduleFactories.get(id);
    }

    void validate(PipelineProfile profile) {
      PassRegistry registry = profile.registry();
      validateFactories(registry, functionFactories, PassDescriptor.Stage.IR_FUNCTION);
      validateFactories(registry, moduleFactories, PassDescriptor.Stage.IR_MODULE);
      for (String id : profile.enabledCandidates()) {
        PassDescriptor descriptor = registry.require(id);
        boolean missing = switch (descriptor.stage()) {
          case IR_FUNCTION -> !functionFactories.containsKey(id);
          case IR_MODULE -> !moduleFactories.containsKey(id);
          case BACKEND_FUNCTION, BACKEND_MODULE -> false;
        };
        if (missing) throw missingFactory(descriptor);
      }
    }

    private static <T> Map<String, T> copyFactories(Map<String, T> source, String name) {
      Objects.requireNonNull(source, name);
      LinkedHashMap<String, T> copy = new LinkedHashMap<>();
      source.forEach((id, factory) -> {
        if (id == null || id.isBlank()) {
          throw new IllegalArgumentException(name + " contains a blank candidate id");
        }
        copy.put(id, Objects.requireNonNull(factory, name + "[" + id + "]"));
      });
      return Map.copyOf(copy);
    }

    private static void validateFactories(
        PassRegistry registry, Map<String, ?> factories, PassDescriptor.Stage stage) {
      for (String id : factories.keySet()) {
        PassDescriptor descriptor = registry.require(id);
        if (!descriptor.candidate()) {
          throw new IllegalArgumentException(
              "candidate provider id is not a CANDIDATE descriptor: " + id);
        }
        if (descriptor.stage() != stage) {
          throw new IllegalArgumentException(
              "candidate provider registered '" + id + "' for " + stage
                  + " but registry declares " + descriptor.stage());
        }
      }
    }
  }

  static final class Schedule {
    private record ScheduledOccurrence(
        PassDescriptor descriptor,
        int occurrence) {}

    private final PipelineProfile profile;
    private final CandidatePassProvider candidatePassProvider;
    private final boolean automaticCandidates;
    private final Map<String, Integer> occurrences = new LinkedHashMap<>();
    private final Map<Object, List<ScheduledOccurrence>> sequences = new IdentityHashMap<>();

    Schedule(PipelineProfile profile) {
      this.profile = Objects.requireNonNull(profile, "profile");
      this.candidatePassProvider = CandidatePassProvider.empty();
      this.automaticCandidates = false;
    }

    Schedule(PipelineProfile profile, CandidatePassProvider candidatePassProvider) {
      this.profile = Objects.requireNonNull(profile, "profile");
      this.candidatePassProvider = Objects.requireNonNull(
          candidatePassProvider, "candidatePassProvider");
      candidatePassProvider.validate(profile);
      this.automaticCandidates = true;
    }

    void function(FunctionPassManager manager, String passId, FunctionPass pass) {
      PassDescriptor descriptor = production(passId, PassDescriptor.Stage.IR_FUNCTION);
      int occurrence = reserve(descriptor);
      automaticFunctionCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.BEFORE);
      record(manager, descriptor, occurrence);
      if (profile.isEnabled(passId, occurrence)) manager.addPass(pass, descriptor, occurrence);
      automaticFunctionCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.AFTER);
    }

    void module(ModulePassManager manager, String passId, ModulePass pass) {
      PassDescriptor descriptor = production(passId, PassDescriptor.Stage.IR_MODULE);
      int occurrence = reserve(descriptor);
      automaticModuleCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.BEFORE);
      record(manager, descriptor, occurrence);
      if (profile.isEnabled(passId, occurrence)) manager.addPass(pass, descriptor, occurrence);
      automaticModuleCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.AFTER);
    }

    void candidateFunction(
        FunctionPassManager manager,
        String passId,
        BiFunction<PassDescriptor, Integer, ? extends FunctionPass> factory) {
      Objects.requireNonNull(factory, "factory");
      PassDescriptor descriptor = candidate(passId, PassDescriptor.Stage.IR_FUNCTION);
      int occurrence = reserve(descriptor);
      record(manager, descriptor, occurrence);
      if (profile.isEnabled(passId, occurrence)) {
        manager.addPass(
            Objects.requireNonNull(
                factory.apply(descriptor, occurrence), "candidate function pass factory result"),
            descriptor,
            occurrence);
      }
    }

    void candidateModule(
        ModulePassManager manager,
        String passId,
        BiFunction<PassDescriptor, Integer, ? extends ModulePass> factory) {
      Objects.requireNonNull(factory, "factory");
      PassDescriptor descriptor = candidate(passId, PassDescriptor.Stage.IR_MODULE);
      int occurrence = reserve(descriptor);
      record(manager, descriptor, occurrence);
      if (profile.isEnabled(passId, occurrence)) {
        manager.addPass(
            Objects.requireNonNull(
                factory.apply(descriptor, occurrence), "candidate module pass factory result"),
            descriptor,
            occurrence);
      }
    }

    void functionObserved(
        FunctionPassManager manager,
        String passId,
        BiFunction<PassDescriptor, Integer, FunctionPass> factory) {
      PassDescriptor descriptor = production(passId, PassDescriptor.Stage.IR_FUNCTION);
      int occurrence = reserve(descriptor);
      automaticFunctionCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.BEFORE);
      record(manager, descriptor, occurrence);
      if (profile.isEnabled(passId, occurrence)) {
        manager.addPass(factory.apply(descriptor, occurrence), descriptor, occurrence);
      }
      automaticFunctionCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.AFTER);
    }

    void moduleObserved(
        ModulePassManager manager,
        String passId,
        BiFunction<PassDescriptor, Integer, ModulePass> factory) {
      PassDescriptor descriptor = production(passId, PassDescriptor.Stage.IR_MODULE);
      int occurrence = reserve(descriptor);
      automaticModuleCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.BEFORE);
      record(manager, descriptor, occurrence);
      if (profile.isEnabled(passId, occurrence)) {
        manager.addPass(factory.apply(descriptor, occurrence), descriptor, occurrence);
      }
      automaticModuleCandidates(
          manager, descriptor, occurrence, PassDescriptor.AnchorPosition.AFTER);
    }

    private void automaticFunctionCandidates(
        FunctionPassManager manager,
        PassDescriptor anchorDescriptor,
        int anchorOccurrence,
        PassDescriptor.AnchorPosition position) {
      if (!automaticCandidates) return;
      for (PassDescriptor candidate : candidatesAt(
          anchorDescriptor, anchorOccurrence, position, PassDescriptor.Stage.IR_FUNCTION)) {
        reserveAutomaticFunctionCandidate(manager, candidate);
      }
    }

    private void automaticModuleCandidates(
        ModulePassManager manager,
        PassDescriptor anchorDescriptor,
        int anchorOccurrence,
        PassDescriptor.AnchorPosition position) {
      if (!automaticCandidates) return;
      for (PassDescriptor candidate : candidatesAt(
          anchorDescriptor, anchorOccurrence, position, PassDescriptor.Stage.IR_MODULE)) {
        reserveAutomaticModuleCandidate(manager, candidate);
      }
    }

    private List<PassDescriptor> candidatesAt(
        PassDescriptor anchorDescriptor,
        int anchorOccurrence,
        PassDescriptor.AnchorPosition position,
        PassDescriptor.Stage stage) {
      return profile.registry().candidates().stream()
          .filter(candidate -> candidate.stage() == stage)
          .filter(candidate -> candidate.candidateAnchor().passId().equals(anchorDescriptor.id()))
          .filter(candidate -> candidate.candidateAnchor().occurrence() == anchorOccurrence)
          .filter(candidate -> candidate.candidateAnchor().position() == position)
          .toList();
    }

    private void reserveAutomaticFunctionCandidate(
        FunctionPassManager manager, PassDescriptor descriptor) {
      int occurrence = reserve(descriptor);
      record(manager, descriptor, occurrence);
      if (!profile.isEnabled(descriptor.id(), occurrence)) return;
      BiFunction<PassDescriptor, Integer, FunctionPass> factory =
          candidatePassProvider.functionFactory(descriptor.id());
      if (factory == null) throw missingFactory(descriptor);
      manager.addPass(
          Objects.requireNonNull(
              factory.apply(descriptor, occurrence), "candidate function pass factory result"),
          descriptor,
          occurrence);
    }

    private void reserveAutomaticModuleCandidate(
        ModulePassManager manager, PassDescriptor descriptor) {
      int occurrence = reserve(descriptor);
      record(manager, descriptor, occurrence);
      if (!profile.isEnabled(descriptor.id(), occurrence)) return;
      BiFunction<PassDescriptor, Integer, ModulePass> factory =
          candidatePassProvider.moduleFactory(descriptor.id());
      if (factory == null) throw missingFactory(descriptor);
      manager.addPass(
          Objects.requireNonNull(
              factory.apply(descriptor, occurrence), "candidate module pass factory result"),
          descriptor,
          occurrence);
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
      verifyCandidateAnchors();
    }

    private PassDescriptor candidate(String id, PassDescriptor.Stage stage) {
      PassDescriptor descriptor = descriptor(id, stage);
      if (!descriptor.candidate()) {
        throw new IllegalArgumentException(
            "candidate scheduling requires a CANDIDATE descriptor: " + id);
      }
      return descriptor;
    }

    private PassDescriptor production(String id, PassDescriptor.Stage stage) {
      PassDescriptor descriptor = descriptor(id, stage);
      if (descriptor.candidate()) {
        throw new IllegalArgumentException(
            "candidate passes must use a lazy candidate scheduling method: " + id);
      }
      return descriptor;
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

    private void record(Object manager, PassDescriptor descriptor, int occurrence) {
      sequences.computeIfAbsent(manager, ignored -> new ArrayList<>())
          .add(new ScheduledOccurrence(descriptor, occurrence));
    }

    private void verifyCandidateAnchors() {
      LinkedHashMap<PassDescriptor.CandidateAnchor, List<String>> expectedGroups =
          new LinkedHashMap<>();
      for (PassDescriptor descriptor : profile.registry().candidates()) {
        if (!descriptor.stage().isIr()) continue;
        expectedGroups.computeIfAbsent(descriptor.candidateAnchor(), ignored -> new ArrayList<>())
            .add(descriptor.id());
      }
      for (Map.Entry<PassDescriptor.CandidateAnchor, List<String>> group
          : expectedGroups.entrySet()) {
        PassDescriptor.CandidateAnchor anchor = group.getKey();
        List<ScheduledOccurrence> anchorSequence = null;
        int anchorIndex = -1;
        for (List<ScheduledOccurrence> sequence : sequences.values()) {
          for (int index = 0; index < sequence.size(); index++) {
            ScheduledOccurrence scheduled = sequence.get(index);
            if (scheduled.descriptor().id().equals(anchor.passId())
                && scheduled.occurrence() == anchor.occurrence()) {
              if (anchorSequence != null) {
                throw new IllegalStateException(
                    "IR candidate anchor is scheduled in more than one pipeline fragment: "
                        + anchor.passId() + "#" + anchor.occurrence());
              }
              anchorSequence = sequence;
              anchorIndex = index;
            }
          }
        }
        List<String> actual = anchorSequence == null
            ? List.of()
            : candidateGroupAtAnchor(anchorSequence, anchorIndex, anchor);
        if (!actual.equals(group.getValue())) {
          throw new IllegalStateException(
              "IR candidate group at " + anchor.passId() + "#" + anchor.occurrence()
                  + " " + anchor.position() + " must follow registry order " + group.getValue()
                  + ", but pipeline schedules " + actual);
        }
      }
    }

    private static List<String> candidateGroupAtAnchor(
        List<ScheduledOccurrence> sequence,
        int anchorIndex,
        PassDescriptor.CandidateAnchor anchor) {
      if (anchor.position() == PassDescriptor.AnchorPosition.BEFORE) {
        int start = anchorIndex;
        while (start > 0 && sharesAnchor(sequence.get(start - 1).descriptor(), anchor)) {
          start--;
        }
        return sequence.subList(start, anchorIndex).stream()
            .map(item -> item.descriptor().id())
            .toList();
      }
      int end = anchorIndex + 1;
      while (end < sequence.size() && sharesAnchor(sequence.get(end).descriptor(), anchor)) {
        end++;
      }
      return sequence.subList(anchorIndex + 1, end).stream()
          .map(item -> item.descriptor().id())
          .toList();
    }

    private static boolean sharesAnchor(
        PassDescriptor descriptor,
        PassDescriptor.CandidateAnchor anchor) {
      return descriptor.candidate() && descriptor.candidateAnchor().equals(anchor);
    }
  }
}
