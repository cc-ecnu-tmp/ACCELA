package accela.backend;

import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.CompareBranchFusion;
import accela.backend.lowering.LoopConditionDuplication;
import accela.backend.lowering.LoopConstantHoisting;
import accela.backend.lowering.MachineBlockPlacement;
import accela.backend.lowering.PhiElimination;
import accela.backend.lowering.SharedReturnBlock;
import accela.backend.lowering.SiblingTailCallFormation;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineModule;
import accela.backend.regalloc.AllSpillRegisterAllocator;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVAllocationRewriter;
import accela.backend.target.RISCVAsmPrinter;
import accela.backend.target.RISCVConstantDivisionLowering;
import accela.backend.target.RISCVFrameLowering;
import accela.backend.target.RISCVTarget;
import java.util.LinkedHashMap;
import java.util.Map;

// TODO: We should register this as a Pass.
final class BackendPipeline {
  private final RISCVTarget target = new RISCVTarget();
  private final IRToMachineLowering lowering = new IRToMachineLowering(target);
  private final PhiElimination phiElimination = new PhiElimination();
  private final CompareBranchFusion compareBranchFusion = new CompareBranchFusion();
  private final LoopConditionDuplication loopConditionDuplication = new LoopConditionDuplication();
  private final SharedReturnBlock sharedReturnBlock = new SharedReturnBlock();
  private final SiblingTailCallFormation siblingTailCalls = new SiblingTailCallFormation();
  private final MachineBlockPlacement blockPlacement = new MachineBlockPlacement();
  private final RISCVConstantDivisionLowering constantDivision = new RISCVConstantDivisionLowering();
  private final LoopConstantHoisting loopConstantHoisting = new LoopConstantHoisting();
  private final RegisterAllocator allocator = new AllSpillRegisterAllocator();
  private final RISCVFrameLowering frameLowering = new RISCVFrameLowering(target);
  private final RISCVAllocationRewriter allocationRewriter = new RISCVAllocationRewriter(target, frameLowering);
  private final RISCVAsmPrinter asmPrinter = new RISCVAsmPrinter(target, frameLowering, allocationRewriter);

  String compileToAssembly(accela.ir.Module module) {
    MachineModule machineModule = lowering.lower(module);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    for (MachineFunction function : machineModule.getFunctions()) {
      phiElimination.run(function);
      compareBranchFusion.run(function);
      loopConditionDuplication.run(function);
      siblingTailCalls.run(function);
      sharedReturnBlock.run(function);
      constantDivision.run(function);
      loopConstantHoisting.run(function);
      blockPlacement.run(function);
      allocations.put(function, allocator.allocate(function, target));
    }

    return asmPrinter.print(machineModule, allocations);
  }
}
