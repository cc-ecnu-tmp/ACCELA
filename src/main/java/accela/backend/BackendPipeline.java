package accela.backend;

import accela.backend.instrument.BackendPassInstrumentation;
import accela.backend.lowering.GlobalAddressMaterialization;
import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.LoopConditionDuplication;
import accela.backend.lowering.MachineBlockPlacement;
import accela.backend.lowering.MachineBranchFolding;
import accela.backend.lowering.MachineCSE;
import accela.backend.lowering.MachineConstantCSE;
import accela.backend.lowering.MachineCopyPropagation;
import accela.backend.lowering.MachineLICM;
import accela.backend.lowering.MemoryAddressFolding;
import accela.backend.lowering.PhiElimination;
import accela.backend.lowering.globalmerge.GlobalMerge;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineModule;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.IteratedRegisterAllocator;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVAsmEmitter;
import accela.backend.target.RISCVAsmPrinter;
import accela.backend.target.RISCVFrameLowering;
import accela.backend.target.RISCVTarget;
import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.PipelineProfile;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.function.BiFunction;
import java.util.function.BooleanSupplier;

/** Registered RISC-V backend pipeline with deterministic ablation points. */
final class BackendPipeline {
  private final PipelineProfile profile;
  private final BackendPassInstrumentation instrumentation;
  private final CandidatePassProvider candidatePassProvider;
  private final RISCVTarget target = new RISCVTarget();
  private final IRToMachineLowering lowering = new IRToMachineLowering(target);
  private final PhiElimination phiElimination = new PhiElimination();
  private final LoopConditionDuplication loopConditionDuplication = new LoopConditionDuplication();
  private final MemoryAddressFolding memoryAddressFolding = new MemoryAddressFolding();
  private final MachineLICM machineLicm = new MachineLICM();
  private final MachineConstantCSE constantCse = new MachineConstantCSE();
  private final GlobalAddressMaterialization globalAddresses = new GlobalAddressMaterialization();
  private final MachineBlockPlacement blockPlacement = new MachineBlockPlacement();
  private final MachineBranchFolding branchFolding = new MachineBranchFolding();
  private final MachineCSE machineCse = new MachineCSE();
  private final MachineCopyPropagation copyPropagation = new MachineCopyPropagation();
  private final RegisterAllocator allocator = new IteratedRegisterAllocator();
  private final RISCVFrameLowering frameLowering = new RISCVFrameLowering(target);

  BackendPipeline(PipelineProfile profile, BackendPassInstrumentation instrumentation) {
    this(profile, instrumentation, CandidatePassProvider.empty());
  }

  BackendPipeline(
      PipelineProfile profile,
      BackendPassInstrumentation instrumentation,
      CandidatePassProvider candidatePassProvider) {
    this.profile = Objects.requireNonNull(profile, "profile");
    this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
    this.candidatePassProvider = Objects.requireNonNull(
        candidatePassProvider, "candidatePassProvider");
    candidatePassProvider.validate(profile);
  }

  String compileToAssembly(accela.ir.Module module) {
    Schedule schedule = new Schedule(profile, candidatePassProvider);
    PassDescriptor loweringPass = schedule.reserve(PassRegistry.BACKEND_IR_LOWERING,
        PassDescriptor.Stage.BACKEND_MODULE);
    MachineModule machineModule = instrumentation.isEnabled()
        ? instrumentation.lower(loweringPass, 1, module, () -> lowering.lower(module))
        : lowering.lower(module);
    GlobalMerge globalMerge = new GlobalMerge(machineModule, target);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    FunctionStage copy = schedule.function(PassRegistry.BACKEND_COPY_PROPAGATION);
    FunctionStage phi = schedule.function(PassRegistry.BACKEND_PHI_ELIMINATION);
    FunctionStage addressFolding = schedule.function(PassRegistry.BACKEND_MEMORY_ADDRESS_FOLDING);
    FunctionStage cse = schedule.function(PassRegistry.BACKEND_MACHINE_CSE);
    FunctionStage merge = schedule.function(PassRegistry.BACKEND_GLOBAL_MERGE);
    FunctionStage licm = schedule.function(PassRegistry.BACKEND_MACHINE_LICM);
    FunctionStage conditionDup = schedule.function(PassRegistry.BACKEND_LOOP_CONDITION_DUPLICATION);
    FunctionStage constCse = schedule.function(PassRegistry.BACKEND_CONSTANT_CSE);
    FunctionStage globalAddress = schedule.function(
        PassRegistry.BACKEND_GLOBAL_ADDRESS_MATERIALIZATION);
    FunctionStage placement1 = schedule.function(PassRegistry.BACKEND_BLOCK_PLACEMENT);
    FunctionStage registerAllocation = schedule.function(PassRegistry.BACKEND_REGISTER_ALLOCATION);
    FunctionStage branch = schedule.function(PassRegistry.BACKEND_BRANCH_FOLDING);
    FunctionStage placement2 = schedule.function(PassRegistry.BACKEND_BLOCK_PLACEMENT);

    for (MachineFunction function : machineModule.getFunctions()) {
      runAnchored(copy, function, () -> copyPropagation.run(function));
      runAnchored(phi, function, () -> phiElimination.run(function));
      runAnchored(addressFolding, function, () -> memoryAddressFolding.run(function));
      runAnchored(cse, function, () -> machineCse.run(function));
      runAnchored(merge, function, () -> globalMerge.run(function));
      runAnchored(licm, function, () -> machineLicm.run(function));
      runAnchored(conditionDup, function, () -> loopConditionDuplication.run(function));
      runAnchored(constCse, function, () -> constantCse.run(function));
      runAnchored(globalAddress, function, () -> globalAddresses.run(function));
      runAnchored(placement1, function, () -> blockPlacement.run(function));
      AllocationResult allocation = allocateAnchored(registerAllocation, function);
      runAnchored(branch, function, () -> branchFolding.run(function, allocation));
      runAnchored(placement2, function, () -> blockPlacement.run(function));
      allocations.put(function, allocation);
    }

    PassDescriptor strengthReduction = schedule.reserve(
        PassRegistry.BACKEND_RISCV_STRENGTH_REDUCTION,
        PassDescriptor.Stage.BACKEND_MODULE);
    boolean strengthReductionEnabled = profile.isEnabled(strengthReduction.id(), 1);
    RISCVAsmEmitter asmEmitter =
        new RISCVAsmEmitter(
            target, frameLowering, strengthReductionEnabled, instrumentation.isEnabled());
    RISCVAsmPrinter asmPrinter = new RISCVAsmPrinter(target, frameLowering, asmEmitter);
    PassDescriptor emission = schedule.reserve(PassRegistry.BACKEND_ASM_EMISSION,
        PassDescriptor.Stage.BACKEND_MODULE);
    schedule.verifyComplete();
    if (!instrumentation.isEnabled()) {
      return asmPrinter.print(machineModule, allocations);
    }
    return instrumentation.emitAssembly(
        emission,
        1,
        machineModule,
        () -> asmPrinter.print(machineModule, allocations),
        asmPrinter::frameLayoutModified,
        () -> {
          if (strengthReductionEnabled) {
            RISCVAsmEmitter.StrengthReductionObservation observation =
                asmEmitter.strengthReductionObservation();
            instrumentation.observeEmbeddedModulePass(
                strengthReduction,
                1,
                machineModule,
                observation.elapsedNanos(),
                observation.applied() > 0,
                Map.of(
                    "candidates", observation.candidates(),
                    "applied", observation.applied()));
          }
        });
  }

  private void runAnchored(
      FunctionStage stage, MachineFunction function, BooleanSupplier operation) {
    runCandidates(stage.beforeCandidates(), function);
    if (profile.isEnabled(stage.descriptor().id(), stage.occurrence())) {
      if (instrumentation.isEnabled()) {
        instrumentation.runFunction(stage.descriptor(), stage.occurrence(), function, operation);
      } else {
        operation.getAsBoolean();
      }
    }
    runCandidates(stage.afterCandidates(), function);
  }

  private AllocationResult allocateAnchored(
      FunctionStage stage, MachineFunction function) {
    runCandidates(stage.beforeCandidates(), function);
    if (!profile.isEnabled(stage.descriptor().id(), stage.occurrence())) {
      throw new IllegalStateException("register allocation cannot be disabled");
    }
    AllocationResult allocation = instrumentation.isEnabled()
        ? instrumentation.allocate(
            stage.descriptor(),
            stage.occurrence(),
            function,
            () -> allocator.allocate(function, target))
        : allocator.allocate(function, target);
    runCandidates(stage.afterCandidates(), function);
    return allocation;
  }

  private void runCandidates(
      List<CandidateStage<CandidateFunctionPass>> candidates, MachineFunction function) {
    for (CandidateStage<CandidateFunctionPass> candidate : candidates) {
      if (instrumentation.isEnabled()) {
        instrumentation.runFunction(
            candidate.descriptor(),
            candidate.occurrence(),
            function,
            () -> candidate.pass().run(function));
      } else {
        candidate.pass().run(function);
      }
    }
  }

  private record FunctionStage(
      PassDescriptor descriptor,
      int occurrence,
      List<CandidateStage<CandidateFunctionPass>> beforeCandidates,
      List<CandidateStage<CandidateFunctionPass>> afterCandidates) {}

  @FunctionalInterface
  interface CandidateFunctionPass {
    boolean run(MachineFunction function);
  }

  /** Typed, immutable factories for candidates inserted into the real backend pipeline. */
  static final class CandidatePassProvider {
    private final Map<String, BiFunction<PassDescriptor, Integer, CandidateFunctionPass>>
        functionFactories;

    CandidatePassProvider(
        Map<String, BiFunction<PassDescriptor, Integer, CandidateFunctionPass>>
            functionFactories) {
      Objects.requireNonNull(functionFactories, "functionFactories");
      LinkedHashMap<String, BiFunction<PassDescriptor, Integer, CandidateFunctionPass>> copy =
          new LinkedHashMap<>();
      functionFactories.forEach((id, factory) -> {
        if (id == null || id.isBlank()) {
          throw new IllegalArgumentException("functionFactories contains a blank candidate id");
        }
        copy.put(id, Objects.requireNonNull(factory, "functionFactories[" + id + "]"));
      });
      this.functionFactories = Map.copyOf(copy);
    }

    static CandidatePassProvider empty() {
      return new CandidatePassProvider(Map.of());
    }

    BiFunction<PassDescriptor, Integer, CandidateFunctionPass> functionFactory(String id) {
      return functionFactories.get(id);
    }

    void validate(PipelineProfile profile) {
      PassRegistry registry = profile.registry();
      for (String id : functionFactories.keySet()) {
        PassDescriptor descriptor = registry.require(id);
        if (!descriptor.candidate()) {
          throw new IllegalArgumentException(
              "candidate provider id is not a CANDIDATE descriptor: " + id);
        }
        if (descriptor.stage() != PassDescriptor.Stage.BACKEND_FUNCTION) {
          throw new IllegalArgumentException(
              "candidate provider registered '" + id + "' for BACKEND_FUNCTION but registry "
                  + "declares " + descriptor.stage());
        }
      }
      for (String id : profile.enabledCandidates()) {
        PassDescriptor descriptor = registry.require(id);
        if (descriptor.stage() == PassDescriptor.Stage.BACKEND_FUNCTION
            && !functionFactories.containsKey(id)) {
          throw new IllegalStateException(
              "enabled candidate '" + id
                  + "' has no registered BACKEND_FUNCTION factory");
        }
      }
    }
  }

  static record CandidateStage<T>(
      PassDescriptor descriptor,
      int occurrence,
      T pass) {}

  static final class Schedule {
    private record ScheduledOccurrence(
        PassDescriptor descriptor,
        int occurrence) {}

    private final PipelineProfile profile;
    private final CandidatePassProvider candidatePassProvider;
    private final boolean automaticCandidates;
    private final Map<String, Integer> counts = new LinkedHashMap<>();
    private final List<ScheduledOccurrence> sequence = new ArrayList<>();

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

    FunctionStage function(String id) {
      PassDescriptor descriptor = production(id, PassDescriptor.Stage.BACKEND_FUNCTION);
      int occurrence = increment(descriptor, PassDescriptor.Stage.BACKEND_FUNCTION);
      if (!automaticCandidates) {
        sequence.add(new ScheduledOccurrence(descriptor, occurrence));
        return new FunctionStage(descriptor, occurrence, List.of(), List.of());
      }
      List<CandidateStage<CandidateFunctionPass>> before = automaticFunctionCandidates(
          descriptor, occurrence, PassDescriptor.AnchorPosition.BEFORE);
      sequence.add(new ScheduledOccurrence(descriptor, occurrence));
      List<CandidateStage<CandidateFunctionPass>> after = automaticFunctionCandidates(
          descriptor, occurrence, PassDescriptor.AnchorPosition.AFTER);
      return new FunctionStage(descriptor, occurrence, before, after);
    }

    <T> Optional<CandidateStage<T>> candidateFunction(
        String id,
        BiFunction<PassDescriptor, Integer, ? extends T> factory) {
      return candidate(id, PassDescriptor.Stage.BACKEND_FUNCTION, factory);
    }

    PassDescriptor reserve(String id, PassDescriptor.Stage stage) {
      PassDescriptor descriptor = production(id, stage);
      return reserve(descriptor, stage);
    }

    private PassDescriptor reserve(PassDescriptor descriptor, PassDescriptor.Stage stage) {
      int count = increment(descriptor, stage);
      sequence.add(new ScheduledOccurrence(descriptor, count));
      return descriptor;
    }

    private PassDescriptor production(String id, PassDescriptor.Stage stage) {
      PassDescriptor descriptor = profile.registry().require(id);
      if (descriptor.candidate()) {
        throw new IllegalArgumentException(
            "candidate passes must use a lazy candidate scheduling method: " + id);
      }
      if (descriptor.stage() != stage) {
        throw new IllegalStateException("pass '" + id + "' registered for " + descriptor.stage()
            + " but scheduled for " + stage);
      }
      return descriptor;
    }

    private int increment(PassDescriptor descriptor, PassDescriptor.Stage stage) {
      String id = descriptor.id();
      if (descriptor.stage() != stage) {
        throw new IllegalStateException("pass '" + id + "' registered for " + descriptor.stage()
            + " but scheduled for " + stage);
      }
      int count = counts.merge(id, 1, Integer::sum);
      if (count > descriptor.fullPipelineOccurrences()) {
        throw new IllegalStateException("pipeline schedules too many occurrences of '" + id + "'");
      }
      return count;
    }

    private List<CandidateStage<CandidateFunctionPass>> automaticFunctionCandidates(
        PassDescriptor anchorDescriptor,
        int anchorOccurrence,
        PassDescriptor.AnchorPosition position) {
      List<CandidateStage<CandidateFunctionPass>> enabled = new ArrayList<>();
      for (PassDescriptor candidate : profile.registry().candidates()) {
        if (candidate.stage() != PassDescriptor.Stage.BACKEND_FUNCTION) continue;
        PassDescriptor.CandidateAnchor anchor = candidate.candidateAnchor();
        if (!anchor.passId().equals(anchorDescriptor.id())
            || anchor.occurrence() != anchorOccurrence
            || anchor.position() != position) {
          continue;
        }
        int occurrence = increment(candidate, PassDescriptor.Stage.BACKEND_FUNCTION);
        sequence.add(new ScheduledOccurrence(candidate, occurrence));
        if (!profile.isEnabled(candidate.id(), occurrence)) continue;
        BiFunction<PassDescriptor, Integer, CandidateFunctionPass> factory =
            candidatePassProvider.functionFactory(candidate.id());
        if (factory == null) {
          throw new IllegalStateException(
              "enabled candidate '" + candidate.id()
                  + "' has no registered BACKEND_FUNCTION factory");
        }
        CandidateFunctionPass pass = Objects.requireNonNull(
            factory.apply(candidate, occurrence), "candidate backend pass factory result");
        enabled.add(new CandidateStage<>(candidate, occurrence, pass));
      }
      return List.copyOf(enabled);
    }

    void verifyComplete() {
      for (PassDescriptor descriptor : profile.registry().all()) {
        if (!descriptor.stage().isBackend()) continue;
        int actual = counts.getOrDefault(descriptor.id(), 0);
        if (actual != descriptor.fullPipelineOccurrences()) {
          throw new IllegalStateException("registered FULL occurrence count for '" + descriptor.id()
              + "' is " + descriptor.fullPipelineOccurrences() + ", but pipeline schedules " + actual);
        }
      }
      verifyCandidateAnchors();
    }

    private <T> Optional<CandidateStage<T>> candidate(
        String id,
        PassDescriptor.Stage stage,
        BiFunction<PassDescriptor, Integer, ? extends T> factory) {
      Objects.requireNonNull(factory, "factory");
      PassDescriptor descriptor = profile.registry().require(id);
      if (!descriptor.candidate()) {
        throw new IllegalArgumentException(
            "candidate scheduling requires a CANDIDATE descriptor: " + id);
      }
      PassDescriptor reserved = reserve(descriptor, stage);
      int occurrence = counts.get(id);
      if (!profile.isEnabled(id, occurrence)) return Optional.empty();
      T pass = Objects.requireNonNull(
          factory.apply(reserved, occurrence), "candidate backend pass factory result");
      return Optional.of(new CandidateStage<>(reserved, occurrence, pass));
    }

    private void verifyCandidateAnchors() {
      LinkedHashMap<PassDescriptor.CandidateAnchor, List<String>> expectedGroups =
          new LinkedHashMap<>();
      for (PassDescriptor descriptor : profile.registry().candidates()) {
        if (!descriptor.stage().isBackend()) continue;
        expectedGroups.computeIfAbsent(descriptor.candidateAnchor(), ignored -> new ArrayList<>())
            .add(descriptor.id());
      }
      for (Map.Entry<PassDescriptor.CandidateAnchor, List<String>> group
          : expectedGroups.entrySet()) {
        PassDescriptor.CandidateAnchor anchor = group.getKey();
        int anchorIndex = -1;
        for (int index = 0; index < sequence.size(); index++) {
          ScheduledOccurrence scheduled = sequence.get(index);
          if (scheduled.descriptor().id().equals(anchor.passId())
              && scheduled.occurrence() == anchor.occurrence()) {
            if (anchorIndex >= 0) {
              throw new IllegalStateException(
                  "backend candidate anchor is scheduled more than once: "
                      + anchor.passId() + "#" + anchor.occurrence());
            }
            anchorIndex = index;
          }
        }
        List<String> actual = anchorIndex < 0
            ? List.of()
            : candidateGroupAtAnchor(anchorIndex, anchor);
        if (!actual.equals(group.getValue())) {
          throw new IllegalStateException(
              "backend candidate group at " + anchor.passId() + "#" + anchor.occurrence()
                  + " " + anchor.position() + " must follow registry order " + group.getValue()
                  + ", but pipeline schedules " + actual);
        }
      }
    }

    private List<String> candidateGroupAtAnchor(
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
