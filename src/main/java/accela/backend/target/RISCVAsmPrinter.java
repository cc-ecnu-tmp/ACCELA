package accela.backend.target;

import accela.backend.machine.ImmOperand;
import accela.backend.machine.MachineBasicBlock;
import accela.backend.machine.MachineFunction;
import accela.backend.machine.MachineInstr;
import accela.backend.machine.MachineModule;
import accela.backend.machine.MachineOpcode;
import accela.backend.regalloc.AllocationResult;
import accela.backend.regalloc.RegisterLocation;
import accela.backend.regalloc.StackLocation;
import accela.backend.regalloc.ValueLocation;
import accela.ir.Constant;
import accela.ir.GlobalVariable;
import accela.ir.Type;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

public final class RISCVAsmPrinter {
  private final RISCVTarget target;
  private final RISCVFrameLowering frameLowering;
  private final RISCVAsmEmitter asmEmitter;

  public RISCVAsmPrinter(
      RISCVTarget target, RISCVFrameLowering frameLowering, RISCVAsmEmitter asmEmitter) {
    this.target = target;
    this.frameLowering = frameLowering;
    this.asmEmitter = asmEmitter;
  }

  public String print(MachineModule module, Map<MachineFunction, AllocationResult> allocations) {
    List<String> lines = new ArrayList<>();
    lines.add(".attribute arch, \"rv64gc\"");
    emitGlobals(module.getSourceModule(), lines);
    lines.add(".text");
    for (MachineFunction function : module.getFunctions()) {
      AllocationResult allocation = allocations.get(function);
      for (accela.backend.machine.PhysicalRegister register : allocation.getUsedCalleeSavedRegisters()) {
        function.getFrameInfo().addCalleeSavedRegister(register);
      }
      frameLowering.finalizeFrame(function);
      emitFunction(function, allocation, lines);
    }
    if (needsMemzeroHelper(module)) emitMemzeroHelper(lines);
    return String.join("\n", lines) + "\n";
  }

  private boolean needsMemzeroHelper(MachineModule module) {
    return module.getFunctions().stream()
        .flatMap(function -> function.getBlocks().stream())
        .flatMap(block -> block.getInstructions().stream())
        .anyMatch(
            instruction ->
                instruction.getOpcode() == MachineOpcode.MEMZERO
                    && target.shouldUseMemzeroHelper(
                        (int) ((ImmOperand) instruction.getOperands().get(1)).getValue()));
  }

  private void emitMemzeroHelper(List<String> lines) {
    lines.add("");
    lines.add(".type __accela_memzero, @function");
    lines.add(".p2align 2");
    lines.add("__accela_memzero:");
    lines.add("  andi t0, a0, 7");
    lines.add("  beqz t0, .L_accela_memzero_aligned");
    lines.add("  sw zero, 0(a0)");
    lines.add("  addi a0, a0, 4");
    lines.add("  addi a1, a1, -4");
    lines.add(".L_accela_memzero_aligned:");
    lines.add("  li t0, 32");
    lines.add("  blt a1, t0, .L_accela_memzero_words");
    lines.add(".L_accela_memzero_loop32:");
    lines.add("  sd zero, 0(a0)");
    lines.add("  sd zero, 8(a0)");
    lines.add("  sd zero, 16(a0)");
    lines.add("  sd zero, 24(a0)");
    lines.add("  addi a0, a0, 32");
    lines.add("  addi a1, a1, -32");
    lines.add("  bge a1, t0, .L_accela_memzero_loop32");
    lines.add(".L_accela_memzero_words:");
    lines.add("  li t0, 8");
    lines.add("  blt a1, t0, .L_accela_memzero_tail");
    lines.add(".L_accela_memzero_loop8:");
    lines.add("  sd zero, 0(a0)");
    lines.add("  addi a0, a0, 8");
    lines.add("  addi a1, a1, -8");
    lines.add("  bge a1, t0, .L_accela_memzero_loop8");
    lines.add(".L_accela_memzero_tail:");
    lines.add("  beqz a1, .L_accela_memzero_done");
    lines.add("  sw zero, 0(a0)");
    lines.add(".L_accela_memzero_done:");
    lines.add("  ret");
    lines.add(".size __accela_memzero, .-__accela_memzero");
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
        asmEmitter.emitInstruction(function, instr, allocation, lines);
      }
    }
  }

  private String labelFor(MachineFunction function, MachineBasicBlock block) {
    return ".L_" + function.getName() + "_" + block.getLabel().replace('.', '_');
  }
}
