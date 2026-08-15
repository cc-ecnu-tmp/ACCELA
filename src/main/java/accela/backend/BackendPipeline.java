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
import accela.cost.TargetProfile;
import java.util.LinkedHashMap;
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

  BackendPipeline(TargetProfile profile, DecisionTraceSink trace) {
    this.profile = profile;
    this.trace = trace;
  }

  String compileToAssembly(accela.ir.Module module) {
    MachineModule machineModule = lowering.lower(module);
    GlobalMerge globalMerge = new GlobalMerge(machineModule, target);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    for (MachineFunction function : machineModule.getFunctions()) {
      MachineCandidateScheduler scheduler = new MachineCandidateScheduler(profile, allocator, target, trace);
      copyPropagation.run(function);
      phiElimination.run(function);
      memoryAddressFolding.run(function);
      machineCse.run(function);
      scheduler.apply("backend.global-merge", "backend.global-merge.address-equivalence",
          function, globalMerge::run);
      scheduler.apply("backend.machine-licm", "backend.machine-licm.loop-invariance",
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
}
