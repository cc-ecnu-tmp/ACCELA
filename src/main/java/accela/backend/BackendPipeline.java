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
import accela.backend.lowering.VectorLegalization;
import accela.backend.lowering.RVVConfigInsertion;
import accela.backend.lowering.VectorConstantMaterialization;
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
import java.util.LinkedHashMap;
import java.util.Map;

// TODO: We should register this as a Pass.
final class BackendPipeline {
  private final RISCVTarget target;
  private final IRToMachineLowering lowering;
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
  private final RVVConfigInsertion rvvConfigInsertion = new RVVConfigInsertion();
  private final VectorConstantMaterialization vectorConstants =
      new VectorConstantMaterialization();
  private final RegisterAllocator allocator = new IteratedRegisterAllocator();
  private final RISCVFrameLowering frameLowering;
  private final RISCVAsmEmitter asmEmitter;
  private final RISCVAsmPrinter asmPrinter;

  BackendPipeline() {
    this(new RISCVTarget());
  }

  BackendPipeline(RISCVTarget target) {
    this.target = target;
    this.lowering = new IRToMachineLowering(target);
    this.frameLowering = new RISCVFrameLowering(target);
    this.asmEmitter = new RISCVAsmEmitter(target, frameLowering);
    this.asmPrinter = new RISCVAsmPrinter(target, frameLowering, asmEmitter);
  }

  String compileToAssembly(accela.ir.Module module) {
    VectorLegalization.run(module, target);
    MachineModule machineModule = lowering.lower(module);
    GlobalMerge globalMerge = new GlobalMerge(machineModule, target);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    for (MachineFunction function : machineModule.getFunctions()) {
      if (target.hasRVV()) vectorConstants.run(function);
      copyPropagation.run(function);
      phiElimination.run(function);
      memoryAddressFolding.run(function);
      machineCse.run(function);
      globalMerge.run(function);
      machineLicm.run(function);
      loopConditionDuplication.run(function);
      constantCse.run(function);
      globalAddresses.run(function);
      blockPlacement.run(function);
      AllocationResult allocation = allocator.allocate(function, target);
      if (target.hasRVV()) rvvConfigInsertion.run(function);
      branchFolding.run(function, allocation);
      blockPlacement.run(function);
      allocations.put(function, allocation);
    }

    return asmPrinter.print(machineModule, allocations);
  }
}
