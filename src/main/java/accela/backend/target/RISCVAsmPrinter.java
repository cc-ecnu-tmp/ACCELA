package accela.backend;

import accela.ir.Constant;
import accela.ir.GlobalVariable;
import accela.ir.Type;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

final class RISCVAsmPrinter {
  private final RISCVTarget target;
  private final RISCVFrameLowering frameLowering;
  private final RISCVAllocationRewriter allocationRewriter;

  RISCVAsmPrinter(RISCVTarget target, RISCVFrameLowering frameLowering, RISCVAllocationRewriter allocationRewriter) {
    this.target = target;
    this.frameLowering = frameLowering;
    this.allocationRewriter = allocationRewriter;
  }

  String print(MachineModule module, Map<MachineFunction, AllocationResult> allocations) {
    List<String> lines = new ArrayList<>();
    lines.add(".attribute arch, \"rv64gc\"");
    emitGlobals(module.getSourceModule(), lines);
    lines.add(".text");
    for (MachineFunction function : module.getFunctions()) {
      frameLowering.finalizeFrame(function);
      emitFunction(function, allocations.get(function), lines);
    }
    return String.join("\n", lines) + "\n";
  }

  private void emitGlobals(accela.ir.Module module, List<String> lines) {
    if (module.getGlobals().isEmpty()) return;
    for (GlobalVariable global : module.getGlobals()) {
      lines.add(global.isConstant() ? ".section .rodata" : ".data");
      lines.add(".globl " + global.getName());
      lines.add(global.getName() + ":");
      emitGlobalConstant(global.getInitializer(), global.getValueType(), lines);
    }
  }

  private void emitGlobalConstant(Constant constant, Type type, List<String> lines) {
    if (constant instanceof Constant.Int) {
      lines.add("  .word " + ((Constant.Int) constant).value);
      return;
    }
    if (constant instanceof Constant.Float) {
      int bits = java.lang.Float.floatToRawIntBits(((Constant.Float) constant).value);
      lines.add("  .word " + bits);
      return;
    }
    if (constant instanceof Constant.Zero) {
      lines.add("  .zero " + target.sizeOfIrType(type));
      return;
    }
    if (constant instanceof Constant.Array) {
      Constant.Array array = (Constant.Array) constant;
      Type elemType = type.innerType;
      for (Constant elem : array.elements) {
        emitGlobalConstant(elem, elemType, lines);
      }
      int declared = array.elements.size();
      int total = type.size;
      for (int i = declared; i < total; i++) {
        emitGlobalConstant(Constant.zero(elemType), elemType, lines);
      }
      return;
    }
    throw new UnsupportedOperationException("Unsupported global initializer: " + constant.getClass().getName());
  }

  private void emitFunction(MachineFunction function, AllocationResult allocation, List<String> lines) {
    lines.add("");
    lines.add(".globl " + function.getName());
    lines.add(function.getName() + ":");
    frameLowering.emitPrologue(function, lines);
    for (MachineBasicBlock block : function.getBlocks()) {
      lines.add(labelFor(function, block) + ":");
      for (MachineInstr instr : block.getInstructions()) {
        allocationRewriter.emitInstruction(function, instr, allocation, lines);
      }
    }
  }

  private String labelFor(MachineFunction function, MachineBasicBlock block) {
    return ".L_" + function.getName() + "_" + block.getLabel().replace('.', '_');
  }
}
