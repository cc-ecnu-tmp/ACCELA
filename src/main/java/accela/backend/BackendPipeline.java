package accela.backend;

import accela.backend.lowering.IRToMachineLowering;
import accela.backend.lowering.MachineConstantCSE;
import accela.backend.lowering.MemoryAddressFolding;
import accela.backend.lowering.PhiElimination;
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
  private final RISCVTarget target = new RISCVTarget();
  private final IRToMachineLowering lowering = new IRToMachineLowering(target);
  private final PhiElimination phiElimination = new PhiElimination();
  private final MemoryAddressFolding memoryAddressFolding = new MemoryAddressFolding();
  private final MachineConstantCSE constantCse = new MachineConstantCSE();
  private final RegisterAllocator allocator = new IteratedRegisterAllocator();
  private final RISCVFrameLowering frameLowering = new RISCVFrameLowering(target);
  private final RISCVAsmEmitter asmEmitter = new RISCVAsmEmitter(target, frameLowering);
  private final RISCVAsmPrinter asmPrinter = new RISCVAsmPrinter(target, frameLowering, asmEmitter);

  String compileToAssembly(accela.ir.Module module) {
    MachineModule machineModule = lowering.lower(module);
    Map<MachineFunction, AllocationResult> allocations = new LinkedHashMap<>();

    for (MachineFunction function : machineModule.getFunctions()) {
      phiElimination.run(function);
      memoryAddressFolding.run(function);
      constantCse.run(function);
      allocations.put(function, allocator.allocate(function, target));
    }

    return asmPrinter.print(machineModule, allocations);
  }
}
