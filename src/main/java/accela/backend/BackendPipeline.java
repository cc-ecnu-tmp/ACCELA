package accela.backend;

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
import accela.backend.machine.AllocatedMachineVerifier;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineModule;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.IteratedRegisterAllocator;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVAsmEmitter;
import accela.backend.target.RISCVAsmPrinter;
import accela.backend.target.RISCVFrameLowering;
import accela.backend.target.RISCVTarget;
import accela.cost.DecisionTraceSink;
import accela.cost.MachineCandidateScheduler;
import accela.cost.LegalityResult;
import accela.cost.R2MachineBeamScheduler;
import accela.cost.TargetProfile;
import accela.pass.PassDescriptor;
import accela.pass.PassRegistry;
import accela.pass.PipelineProfile;
import accela.pass.R2PassOccurrence;
import accela.pass.R2PassRegistry;
import accela.pass.R2PipelineProfile;
import accela.pass.R2ScheduleState;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

// TODO: We should register this as a Pass.
final class BackendPipeline {
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
  private final RISCVAsmEmitter asmEmitter = new RISCVAsmEmitter(target, frameLowering);
  private final RISCVAsmPrinter asmPrinter = new RISCVAsmPrinter(target, frameLowering, asmEmitter);
  private final TargetProfile profile;
  private final DecisionTraceSink trace;
  private final R2PipelineProfile r2Profile;

  BackendPipeline(TargetProfile profile, DecisionTraceSink trace) {
    this(profile, trace, null);
  }

  BackendPipeline(TargetProfile profile, DecisionTraceSink trace, R2PipelineProfile r2Profile) {
    this.profile = profile;
    this.trace = trace;
    this.r2Profile = r2Profile;
    if (r2Profile != null
        && !r2Profile.applied().equals(r2Profile.registry().fullDecisionOrder())) {
      throw new IllegalArgumentException(
          "dynamic R2 backend scheduling requires the complete production registry");
    }
  }

  String compileToAssembly(accela.ir.Module module) {
    if (r2Profile != null) return compileR2(module);
    MachineModule machineModule = lowering.lower(module);
    GlobalMerge globalMerge = new GlobalMerge(machineModule, target);
    PipelineProfile pipeline = PipelineProfile.r1();
    PassDescriptor globalMergePass = pipeline.require(PassRegistry.GLOBAL_MERGE);
    PassDescriptor machineLicmPass = pipeline.require(PassRegistry.MACHINE_LICM);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    for (MachineFunction function : machineModule.getFunctions()) {
      MachineCandidateScheduler scheduler = new MachineCandidateScheduler(profile, allocator, target, trace);
      copyPropagation.run(function);
      phiElimination.run(function);
      memoryAddressFolding.run(function);
      machineCse.run(function);
      // Preserve the production FULL baseline first.  The scheduler may only consider an
      // additional idempotent application; rejecting it must never remove the established pass.
      globalMerge.run(function);
      scheduler.apply(globalMergePass.id(), new LegalityResult(LegalityResult.Status.PROVED,
              globalMergePass.primaryObligation(), "address equivalence proved by matcher"),
          function, globalMerge::run);
      machineLicm.run(function);
      scheduler.apply(machineLicmPass.id(), new LegalityResult(LegalityResult.Status.PROVED,
              machineLicmPass.primaryObligation(), "loop invariance proved by matcher"),
          function, machineLicm::run);
      loopConditionDuplication.run(function);
      constantCse.run(function);
      globalAddresses.run(function);
      blockPlacement.run(function);
      AllocationResult allocation = allocator.allocate(function, target);
      branchFolding.run(function, allocation);
      blockPlacement.run(function);
      allocations.put(function, allocation);
    }

    return asmPrinter.print(machineModule, allocations);
  }

  private String compileR2(accela.ir.Module module) {
    MachineModule machineModule = lowering.lower(module);
    emitRequiredBoundary(R2PassRegistry.LOWERING, "module");
    GlobalMerge globalMerge = new GlobalMerge(machineModule, target);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();
    for (MachineFunction function : machineModule.getFunctions()) {
      R2MachineBeamScheduler scheduler =
          new R2MachineBeamScheduler(profile, trace, allocator, target);
      List<R2MachineBeamScheduler.Step> steps = List.of(
          step(R2PassRegistry.COPY_PROPAGATION, copyPropagation::run),
          step(R2PassRegistry.PHI_ELIMINATION, candidate -> {
            boolean changed = candidate.getBlocks().stream()
                .flatMap(block -> block.getInstructions().stream())
                .anyMatch(instruction -> instruction.getOpcode()
                    == accela.backend.machine.MachineOpcode.PHI);
            phiElimination.run(candidate);
            return changed;
          }),
          step(R2PassRegistry.ADDRESS_FOLDING, memoryAddressFolding::run),
          step(R2PassRegistry.MACHINE_CSE, machineCse::run),
          step(R2PassRegistry.GLOBAL_MERGE, globalMerge::run),
          step(R2PassRegistry.MACHINE_LICM, machineLicm::run),
          step(R2PassRegistry.LOOP_CONDITION_DUPLICATION, loopConditionDuplication::run),
          step(R2PassRegistry.CONSTANT_CSE, constantCse::run),
          step(R2PassRegistry.GLOBAL_ADDRESS, globalAddresses::run),
          step(R2PassRegistry.BLOCK_PLACEMENT, blockPlacement::run));
      List<String> registeredMir = R2PassRegistry.production().all().stream()
          .filter(occurrence -> occurrence.stage() == accela.pass.PassDescriptor.Stage.MIR)
          .map(R2PassOccurrence::id).toList();
      List<String> executableMir = steps.stream().map(step -> step.occurrence().id()).toList();
      if (!registeredMir.equals(executableMir)) {
        throw new IllegalStateException("R2 MIR registry/executor drift: registered="
            + registeredMir + ", executable=" + executableMir);
      }
      R2MachineBeamScheduler.Plan plan = scheduler.schedule(function, steps,
          branchFolding::run, blockPlacement::run);
      function.replaceWith(plan.preRaFunction());
      AllocationResult allocation = allocator.allocate(function, target);
      emitRequiredBoundary(R2PassRegistry.REGISTER_ALLOCATION, function.getName());
      if (plan.branchFolding()) branchFolding.run(function, allocation);
      if (plan.postRaPlacement()) blockPlacement.run(function);
      AllocatedMachineVerifier.verify(function, allocation);
      allocations.put(function, allocation);
    }
    String assembly = asmPrinter.print(machineModule, allocations);
    emitRequiredBoundary(R2PassRegistry.EMISSION, "module");
    return assembly;
  }

  private static R2MachineBeamScheduler.Step step(
      String family, R2MachineBeamScheduler.Transform transform) {
    return new R2MachineBeamScheduler.Step(
        R2PassRegistry.production().family(family).getFirst(), transform);
  }

  private void emitRequiredBoundary(String family, String targetName) {
    R2PassOccurrence occurrence = R2PassRegistry.production().family(family).getFirst();
    trace.accept(new DecisionTraceSink.Decision(profile.id(),
        profile.evidenceLevel().name().toLowerCase(), occurrence.id(),
        occurrence.scope() == R2PassOccurrence.Scope.MODULE ? "module" : "machine-function",
        targetName, "applied", "required_boundary", "proved",
        occurrence.legalityObligation(), Map.of("required", "true"), null, null, null,
        0, occurrence.scope() == R2PassOccurrence.Scope.MODULE
            ? profile.scheduler().maxModuleExpansions()
            : profile.scheduler().maxFunctionExpansions()));
  }
}
