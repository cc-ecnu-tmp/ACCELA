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
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.function.BooleanSupplier;

/** Registered RISC-V backend pipeline with deterministic ablation points. */
final class BackendPipeline {
  private final PipelineProfile profile;
  private final BackendPassInstrumentation instrumentation;
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
    this.profile = Objects.requireNonNull(profile, "profile");
    this.instrumentation = Objects.requireNonNull(instrumentation, "instrumentation");
  }

  String compileToAssembly(accela.ir.Module module) {
    Schedule schedule = new Schedule(profile);
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
      run(copy, function, () -> copyPropagation.run(function));
      run(phi, function, () -> phiElimination.run(function));
      run(addressFolding, function, () -> memoryAddressFolding.run(function));
      run(cse, function, () -> machineCse.run(function));
      run(merge, function, () -> globalMerge.run(function));
      run(licm, function, () -> machineLicm.run(function));
      run(conditionDup, function, () -> loopConditionDuplication.run(function));
      run(constCse, function, () -> constantCse.run(function));
      run(globalAddress, function, () -> globalAddresses.run(function));
      run(placement1, function, () -> blockPlacement.run(function));
      AllocationResult allocation = instrumentation.isEnabled()
          ? instrumentation.allocate(
              registerAllocation.descriptor(),
              registerAllocation.occurrence(),
              function,
              () -> allocator.allocate(function, target))
          : allocator.allocate(function, target);
      run(branch, function, () -> branchFolding.run(function, allocation));
      run(placement2, function, () -> blockPlacement.run(function));
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

  private void run(
      FunctionStage stage, MachineFunction function, BooleanSupplier operation) {
    if (profile.isEnabled(stage.descriptor().id(), stage.occurrence())) {
      if (instrumentation.isEnabled()) {
        instrumentation.runFunction(stage.descriptor(), stage.occurrence(), function, operation);
      } else {
        operation.getAsBoolean();
      }
    }
  }

  private record FunctionStage(PassDescriptor descriptor, int occurrence) {}

  private static final class Schedule {
    private final PipelineProfile profile;
    private final Map<String, Integer> counts = new LinkedHashMap<>();

    Schedule(PipelineProfile profile) {
      this.profile = profile;
    }

    FunctionStage function(String id) {
      PassDescriptor descriptor = reserve(id, PassDescriptor.Stage.BACKEND_FUNCTION);
      return new FunctionStage(descriptor, counts.get(id));
    }

    PassDescriptor reserve(String id, PassDescriptor.Stage stage) {
      PassDescriptor descriptor = profile.registry().require(id);
      if (descriptor.stage() != stage) {
        throw new IllegalStateException("pass '" + id + "' registered for " + descriptor.stage()
            + " but scheduled for " + stage);
      }
      int count = counts.merge(id, 1, Integer::sum);
      if (count > descriptor.fullPipelineOccurrences()) {
        throw new IllegalStateException("pipeline schedules too many occurrences of '" + id + "'");
      }
      return descriptor;
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
    }
  }
}
