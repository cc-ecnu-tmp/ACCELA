package accela.backend;

import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.PhiElimination;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineModule;
import accela.backend.regalloc.AllSpillRegisterAllocator;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterAllocator;
import accela.backend.target.RISCVAllocationRewriter;
import accela.backend.target.RISCVAsmPrinter;
import accela.backend.target.RISCVFrameLowering;
import accela.backend.target.RISCVTarget;
import java.util.LinkedHashMap;
import java.util.Map;

// TODO: We should register this as a Pass.
final class BackendPipeline {
  private final RISCVTarget target = new RISCVTarget();
  private final IRToMachineLowering lowering = new IRToMachineLowering(target);
  private final PhiElimination phiElimination = new PhiElimination();
  private final RegisterAllocator allocator = new AllSpillRegisterAllocator();
  private final RISCVFrameLowering frameLowering = new RISCVFrameLowering(target);
  private final RISCVAllocationRewriter allocationRewriter = new RISCVAllocationRewriter(target, frameLowering);
  private final RISCVAsmPrinter asmPrinter = new RISCVAsmPrinter(target, frameLowering, allocationRewriter);

  String compileToAssembly(accela.ir.Module module) {
    MachineModule machineModule = lowering.lower(module);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    for (MachineFunction function : machineModule.getFunctions()) {
      phiElimination.run(function);
      allocations.put(function, allocator.allocate(function, target));
    }

    return asmPrinter.print(machineModule, allocations);
  }
}
